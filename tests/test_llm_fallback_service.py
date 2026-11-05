# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Engineer end-to-end regressions through file-loaded service configuration."""

import json

import httpx
import pytest
import yaml

from nomos.config.settings import load_config
from nomos.llm.limiter import EndpointLimiter, LimiterPool
from nomos.nat import functions
from nomos.nat.functions import AuthorizeInput, CouncilService, ProposeIntentInput

STUB_API_KEY = "test"


def write_config(tmp_path, *, chairman=False, review=False):
    path = tmp_path / "engineer-council.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "council_id": "engineer-council",
                "council_key_path": str(tmp_path / "keys-engineer"),
                "require_auth": False,
                "opa": {"enabled": False},
                "llm_retry_budget_seconds": 3,
                "llm_backends": {
                    "primary": {
                        "provider": "openai",
                        "model": "configured-primary",
                        "api_key": STUB_API_KEY,
                        "request_timeout_seconds": 1,
                    },
                    "backup": {
                        "provider": "anthropic",
                        "model": "configured-backup",
                        "api_key": STUB_API_KEY,
                        "request_timeout_seconds": 1,
                    },
                },
                "llm_fallbacks": {"primary": ["backup"]},
                "members": [{"id": "reviewer", "backend": "primary"}],
                "chairman": {"enabled": chairman, "backend": "primary"},
                "code_review": {"enabled": review, "backend": "primary"},
            }
        )
    )
    return path


def success_wire(text):
    return {
        "id": "msg-engineer",
        "type": "message",
        "role": "assistant",
        "model": "returned-backup",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 11, "output_tokens": 5},
    }


@pytest.mark.parametrize("tool", ["propose", "authorize"])
@pytest.mark.parametrize(
    "answer,approved", [("approve", True), ("deny", False), ("malformed", False)]
)
@pytest.mark.asyncio
async def test_file_loaded_service_outage_backup_decision_and_ledger(
    tmp_path,
    monkeypatch,
    tool,
    answer,
    approved,
):
    wire, clients = [], []
    original_factory = functions.create_backend

    def handle(request):
        wire.append(request)
        if "openai" in request.url.host:
            return httpx.Response(503, json={"error": {"message": "stub outage"}})
        text = (
            "unparseable"
            if answer == "malformed"
            else json.dumps(
                {
                    "decision": answer,
                    "reasoning": "Specific service stub review.",
                    "execution": {"selected_backend": "forged"},
                }
            )
        )
        return httpx.Response(200, json=success_wire(text))

    def factory(config, limiter_pool=None):
        backend = original_factory(config, limiter_pool)
        # Keep the real SDK client and its lock-version request/response handling.
        inner = backend._inner
        previous = inner._client
        clients.append(previous)
        client_type = type(previous)
        inner._client = client_type(
            api_key=STUB_API_KEY,
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        )
        clients.append(inner._client)
        return backend

    monkeypatch.setattr(functions, "create_backend", factory)
    service = CouncilService(load_config(str(write_config(tmp_path))))
    captured = []
    monkeypatch.setattr(service._telemetry, "publish_later", captured.append)
    try:
        request = {
            "description": "Inspect one status file",
            "justification": "Check status",
            "target_resource": "/workspace/status",
            "command": "/bin/cat /workspace/status",
            "working_directory": "/workspace",
        }
        if tool == "propose":
            result = await service.propose_intent(
                ProposeIntentInput(agent_id="engineer", **request)
            )
            assert (result.state == "approved") is approved
        else:
            result = await service.authorize(AuthorizeInput(agent_id="engineer", **request))
            assert (result.status == "approved") is approved
        assert len(wire) == 2
        actors = captured[-1]["actors"]
        actor = next(a for a in actors if a["role"] == "council_member")
        assert actor["provider"] == "anthropic" and actor["backend"] == "backup"
        assert actor["configured_model"] == "configured-backup"
        assert actor["returned_model"] == "returned-backup"
        assert actor["llm_attempts"][0]["outcome"] == "unavailable"
        assert actor["llm_attempts"][1]["outcome"] == (
            "success" if answer != "malformed" else "invalid_response"
        )
        assert "execution" not in result.model_dump_json()
        ledger = service.council.ledger
        entries = [ledger.get(n) for n in range(ledger.length())]
        review = next(entry for entry in entries if entry and "timing" in entry.payload)
        assert review.payload["timing"]["actors"] == actors
    finally:
        for client in clients:
            await client.close()


def test_overload_gates_required_routes_and_preserves_singleton_ewma():
    service = CouncilService.__new__(CouncilService)
    service._settings = type("Config", (), {"council_id": "engineer"})()
    primary, backup = EndpointLimiter(1, 2), EndpointLimiter(1, 2)
    service._limiters = [primary, backup]
    service._limiter_routes = [(primary, backup)]
    service._intent_latency_ewma_s = 100
    backup._latency_ewma = 1
    assert service._overload_rejection() is None
    primary._latency_ewma, backup._latency_ewma = 1, 3
    assert service._overload_rejection() is None
    primary._latency_ewma = 3
    assert service._overload_rejection() is not None
    # A required single backend remains required even if a different route is healthy.
    backup._latency_ewma = 1
    service._limiter_routes.append((primary,))
    assert service._overload_rejection() is not None


@pytest.mark.asyncio
async def test_shared_pool_keeps_primary_and_backup_endpoint_limits(tmp_path, monkeypatch):
    clients = []
    original_factory = functions.create_backend

    def factory(config, limiter_pool=None):
        backend = original_factory(config, limiter_pool)
        clients.append(backend._inner._client)
        return backend

    monkeypatch.setattr(functions, "create_backend", factory)
    settings = load_config(str(write_config(tmp_path, chairman=True, review=True)))
    pool = LimiterPool()
    first, second = CouncilService(settings, pool), CouncilService(settings, pool)
    try:
        member = first._agents[0]._call_candidates
        other = second._agents[0]._call_candidates
        assert len(first._limiters) == len(second._limiters) == 2
        assert member[0].backend.limiter is other[0].backend.limiter
        assert member[1].backend.limiter is other[1].backend.limiter
        assert member[0].backend.limiter is not member[1].backend.limiter
        assert first._chairman._call_candidates[1].backend.limiter is member[1].backend.limiter
        assert first._code_reviewer._call_candidates[1].backend.limiter is member[1].backend.limiter
    finally:
        for client in clients:
            await client.close()


def test_active_backup_credential_preflight(tmp_path, monkeypatch):
    path = write_config(tmp_path)
    raw = yaml.safe_load(path.read_text())
    raw["llm_backends"]["backup"]["api_key"] = ""
    path.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv("NOMOS_DEPLOY_PROFILE", "alpha")
    with pytest.raises(ValueError, match=r"llm_backends\.backup\.api_key"):
        load_config(str(path))
