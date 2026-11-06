# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Independent F04/F06/F10-F16 actual-SDK and complete-service contracts."""

import asyncio
import errno
import importlib
import json
import ssl

import httpx
import pytest

from nomos.config.settings import CouncilSettings, LLMBackendConfig, load_config
from nomos.llm import limiter as limiter_module
from nomos.llm.base import LLMRateLimitError, LLMTimeoutError
from nomos.llm.council_agent import create_backend
from nomos.llm.limiter import LimitedBackend, LimiterPool
from nomos.nat.functions import AuthorizeInput, CouncilService, ProposeIntentInput
from tests.test_approved_fallback_contract import (
    APPROVE,
    CHAIR,
    DENY,
    ScriptBackend,
    candidates,
    complete,
    intent,
    member,
    review_json,
)
from tests.test_approved_fallback_contract_config import config_data


def wire_response(provider, text=APPROVE, model="wire-backup-model"):
    if provider == "openai":
        return {
            "id": "offline-chat",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        }
    return {
        "id": "offline-message",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 11, "output_tokens": 7},
    }


@pytest.fixture
def sdk_clients(monkeypatch):
    """Actual installed SDKs, with every HTTP request confined to MockTransport."""
    clients = []

    def install(provider, handler):
        sdk = importlib.import_module(provider)
        class_name = "AsyncOpenAI" if provider == "openai" else "AsyncAnthropic"
        original = getattr(sdk, class_name)

        def constructor(**kwargs):
            client = original(
                **kwargs,
                http_client=httpx.AsyncClient(
                    transport=httpx.MockTransport(handler),
                    trust_env=False,
                ),
            )
            clients.append(client)
            return client

        monkeypatch.setattr(sdk, class_name, constructor)

    return install, clients


async def close_clients(clients):
    await asyncio.gather(*(client.close() for client in clients))


def sdk_backend(provider, name, pool=None):
    return create_backend(
        LLMBackendConfig(
            provider=provider,
            model=name,
            api_key="offline-test-key",  # pragma: allowlist secret - inert offline fixture
            base_url=f"https://{name}.invalid/v1",
            sdk_max_retries=0,
            request_timeout_seconds=0.5,
        ),
        limiter_pool=pool,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
async def test_actual_sdk_transient_http_status_selects_backup_once(sdk_clients, provider, status):
    install, clients = sdk_clients
    wire = []

    async def handler(request):
        wire.append(request)
        if request.url.host == "primary.invalid":
            return httpx.Response(
                status,
                json={"error": {"type": "api_error", "message": "offline"}},
                headers={"retry-after": "0"},
            )
        return httpx.Response(200, json=wire_response(provider))

    install(provider, handler)
    primary = sdk_backend(provider, "primary", LimiterPool())
    backup = sdk_backend(provider, "backup", LimiterPool())
    try:
        result = await member(primary, candidates(primary, backup, timeout=3)).evaluate_intent(
            intent()
        )
        assert result.decision == "approve", f"F14 {provider=} {status=} must select backup"
        assert [r.url.host for r in wire] == ["primary.invalid", "backup.invalid"], (
            f"F14 {provider=} {status=} SDK retries=0 and each candidate once"
        )
        assert result.execution.attempts[-1].returned_model == "wire-backup-model"
        expected_outcomes = {"rate_limit"} if status == 429 else {"timeout", "unavailable"}
        assert result.execution.attempts[0].outcome in expected_outcomes, (
            f"F14 {provider=} {status=} classification"
        )
    finally:
        await close_clients(clients)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 501])
async def test_actual_sdk_terminal_http_status_never_changes_model(sdk_clients, provider, status):
    install, clients = sdk_clients
    wire = []

    async def handler(request):
        wire.append(request)
        return httpx.Response(
            status, json={"error": {"type": "invalid_request_error", "message": "offline"}}
        )

    install(provider, handler)
    primary = sdk_backend(provider, "primary", LimiterPool())
    backup = sdk_backend(provider, "backup", LimiterPool())
    try:
        result = await member(primary, candidates(primary, backup, timeout=3)).evaluate_intent(
            intent()
        )
        assert result.decision == "deny", f"F06 {provider=} {status=} must deny"
        assert [r.url.host for r in wire] == ["primary.invalid"], (
            f"F06 {provider=} {status=} zero fallback"
        )
        assert result.execution.selected_backend is None, f"F06 {provider=} {status=} no selection"
    finally:
        await close_clients(clients)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_actual_sdk_connection_refused_is_available_for_approved_fallback(
    sdk_clients, provider
):
    install, clients = sdk_clients
    wire = []

    async def handler(request):
        wire.append(request)
        if request.url.host == "primary.invalid":
            cause = ConnectionRefusedError(errno.ECONNREFUSED, "offline refusal")
            raise httpx.ConnectError("offline transport", request=request) from cause
        return httpx.Response(200, json=wire_response(provider))

    install(provider, handler)
    try:
        primary, backup = sdk_backend(provider, "primary"), sdk_backend(provider, "backup")
        result = await member(primary, candidates(primary, backup)).evaluate_intent(intent())
        assert result.decision == "approve", f"F14 {provider=} recognized connection refusal"
        assert len(wire) == 2, f"F14 {provider=} one wire request per candidate"
    finally:
        await close_clients(clients)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("body", ["malformed-sdk", "malformed-json"])
async def test_actual_sdk_success_with_invalid_output_is_terminal(sdk_clients, provider, body):
    install, clients = sdk_clients
    wire = []

    async def handler(request):
        wire.append(request)
        payload = (
            {"unexpected": "shape"} if body == "malformed-sdk" else wire_response(provider, "{")
        )
        return httpx.Response(200, json=payload)

    install(provider, handler)
    try:
        primary, backup = sdk_backend(provider, "primary"), sdk_backend(provider, "backup")
        result = await member(primary, candidates(primary, backup)).evaluate_intent(intent())
        assert result.decision == "deny", f"F14 {provider=} {body=} invalid output fail-closed"
        assert len(wire) == 1, f"F14 {provider=} {body=} no model change or SDK retry"
    finally:
        await close_clients(clients)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_actual_sdk_cancellation_drains_transport_and_starts_no_backup(sdk_clients, provider):
    install, clients = sdk_clients
    entered, drained = asyncio.Event(), asyncio.Event()
    wire = []

    async def handler(request):
        wire.append(request)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    install(provider, handler)
    primary, backup = sdk_backend(provider, "primary"), sdk_backend(provider, "backup")
    task = asyncio.create_task(complete(candidates(primary, backup, timeout=3)))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel("sdk-qa-sentinel")
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert caught.value.args == ("sdk-qa-sentinel",), f"F14 {provider=} same cancellation"
        assert drained.is_set() and len(wire) == 1, f"F14 {provider=} drained with zero backup"
        assert caught.value.llm_execution.attempts[-1].outcome == "cancelled"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await close_clients(clients)


@pytest.mark.parametrize(
    "cause",
    [
        ssl.SSLCertVerificationError("offline certificate"),
        httpx.InvalidURL("offline URL"),
        httpx.UnsupportedProtocol("offline protocol"),
        httpx.ProxyError("offline proxy config"),
        RuntimeError("unknown transport reason"),
    ],
)
def test_actual_sdk_connection_wrapper_does_not_hide_terminal_cause(cause):
    import openai

    classify = getattr(limiter_module, "classify_availability_error", None)
    assert classify is not None, "F06 availability classifier is part of public plan"
    request = httpx.Request("POST", "https://offline.invalid/v1")
    wrapped = openai.APIConnectionError(request=request)
    wrapped.__cause__ = cause
    assert classify(wrapped) is None, f"F06 {type(cause).__name__} wrapped by real SDK is terminal"


def test_normalized_availability_cannot_override_terminal_auth_status():
    import openai

    classify = getattr(limiter_module, "classify_availability_error", None)
    assert classify is not None, "F06 public availability classifier required"
    request = httpx.Request("POST", "https://offline.invalid/v1")
    auth = openai.AuthenticationError(
        "offline", response=httpx.Response(401, request=request), body={}
    )
    for wrapped in (LLMTimeoutError("normalized"), LLMRateLimitError("normalized")):
        wrapped.__cause__ = auth
        assert classify(wrapped) is None, (
            "F06 normalized availability chained to 401 stays terminal"
        )


@pytest.mark.asyncio
async def test_class_name_lookalike_cannot_trigger_fallback_through_limited_backend():
    class APITimeoutError(Exception):
        pass

    primary = LimitedBackend(
        ScriptBackend("primary", [APITimeoutError("fake SDK name")]),
        limiter_module.EndpointLimiter(1),
    )
    backup = ScriptBackend("backup")
    result = await member(primary, candidates(primary, backup)).evaluate_intent(intent())
    assert result.decision == "deny" and backup.calls == [], "F06 fake APITimeoutError is terminal"


def service_data(tmp_path, routes=True):
    data = config_data()
    data.update(
        require_auth=False,
        council_key_path=str(tmp_path / "keys"),
        ledger_path=str(tmp_path / "qa-ledger.jsonl"),
        opa={"enabled": False},
    )
    if not routes:
        data.pop("llm_fallbacks")
    return data


def test_missing_backup_sdk_stops_construction_without_provider_request(tmp_path, monkeypatch):
    import nomos.llm.council_agent as agent_module

    data = service_data(tmp_path)
    data["llm_fallbacks"] = {"primary": ["backup"]}
    data["llm_backends"]["backup"]["provider"] = "anthropic"
    constructed = []

    def backend_class(provider):
        if provider == "anthropic":
            raise ImportError("offline missing backup SDK")

        class OfflineBackend(ScriptBackend):
            def __init__(self, **kwargs):
                super().__init__("openai")
                constructed.append(self)

        return OfflineBackend

    monkeypatch.setattr(agent_module, "_backend_class", backend_class)
    with pytest.raises(ImportError, match="missing backup SDK"):
        CouncilService(CouncilSettings.model_validate(data))
    assert all(not b.calls for b in constructed), "F04 no provider request during startup failure"


def test_unconfigured_service_does_not_instantiate_unused_backends(tmp_path, monkeypatch):
    import nomos.llm.council_agent as agent_module

    constructed = []

    class OfflineBackend(ScriptBackend):
        def __init__(self, **kwargs):
            constructed.append(kwargs["model"])
            super().__init__("openai")

    monkeypatch.setattr(agent_module, "_backend_class", lambda provider: OfflineBackend)
    CouncilService(CouncilSettings.model_validate(service_data(tmp_path, routes=False)))
    assert constructed == ["primary-model"], (
        "F01 defining unused backends does not instantiate them"
    )


@pytest.mark.parametrize(
    "busy_primary,busy_backup,reject",
    [(True, False, False), (False, True, False), (True, True, True)],
)
def test_overload_gate_considers_healthy_candidate_in_required_route(
    tmp_path,
    monkeypatch,
    busy_primary,
    busy_backup,
    reject,
):
    import nomos.llm.council_agent as agent_module

    class OfflineBackend(ScriptBackend):
        def __init__(self, **kwargs):
            super().__init__("openai")

    monkeypatch.setattr(agent_module, "_backend_class", lambda provider: OfflineBackend)
    data = service_data(tmp_path)
    data["llm_fallbacks"] = {"primary": ["backup"]}
    for name, cfg in data["llm_backends"].items():
        cfg.update(
            base_url=f"https://{name}.invalid/v1",
            max_concurrent=1,
            overload_latency_threshold_seconds=10,
        )
    service = CouncilService(CouncilSettings.model_validate(data))
    route = service._create_backend_candidates("primary")
    for entry, busy in zip(route, (busy_primary, busy_backup), strict=True):
        entry.backend.limiter.waiting = 10 if busy else 0
    # A large unrelated total-intent EWMA must not override per-endpoint route projections.
    service._intent_latency_ewma_s = 100
    assert (service._overload_rejection() is not None) == reject, (
        f"F11 {busy_primary=} {busy_backup=} any healthy candidate admits route"
    )


def test_shared_pool_survives_service_replacement_and_old_route_snapshot(tmp_path, monkeypatch):
    import nomos.llm.council_agent as agent_module

    class OfflineBackend(ScriptBackend):
        def __init__(self, **kwargs):
            super().__init__("openai")

    monkeypatch.setattr(agent_module, "_backend_class", lambda provider: OfflineBackend)
    pool = LimiterPool()
    data = service_data(tmp_path)
    data["llm_fallbacks"] = {"primary": ["backup"]}
    for name, cfg in data["llm_backends"].items():
        cfg["base_url"] = f"https://{name}.invalid/v1"
    first = CouncilService(CouncilSettings.model_validate(data), limiter_pool=pool)
    old = first._create_backend_candidates("primary")
    data["llm_fallbacks"] = {"primary": ["second"]}
    second = CouncilService(CouncilSettings.model_validate(data), limiter_pool=pool)
    new = second._create_backend_candidates("primary")
    assert [c.backend_id for c in old] == ["primary", "backup"], "F10 original route retained"
    assert [c.backend_id for c in new] == ["primary", "second"], "F10 new route uses new approval"
    assert old[0].backend.limiter is new[0].backend.limiter, "F10 endpoint limiter survives reload"
    assert old[1].backend.limiter is not new[1].backend.limiter, "F10 distinct endpoint isolation"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,mode",
    [
        ("propose", "approve"),
        ("authorize", "approve"),
        ("propose", "deny"),
        ("authorize", "deny"),
        ("propose", "malformed"),
        ("authorize", "malformed"),
    ],
)
async def test_file_loaded_service_http_outage_preserves_full_review_and_ledger_attribution(
    tmp_path,
    sdk_clients,
    tool,
    mode,
):
    install, clients = sdk_clients
    wire = []
    request = intent(packaged=True)

    async def handler(http_request):
        body = json.loads(http_request.content)
        model = body["model"]
        wire.append(model)
        if model.endswith("primary"):
            return httpx.Response(503, json={"error": {"message": "offline", "type": "api_error"}})
        text = (
            CHAIR
            if model.startswith("chair")
            else review_json(request)
            if model.startswith("review")
            else APPROVE
            if mode == "approve"
            else DENY
            if mode == "deny"
            else "{"
        )
        return httpx.Response(200, json=wire_response("openai", text, model + "-wire"))

    install("openai", handler)
    data = service_data(tmp_path)
    data["llm_backends"] = {
        f"{role}-{position}": {
            "provider": "openai",
            "model": f"{role}-{position}",
            "api_key": "offline-key",  # pragma: allowlist secret - inert offline fixture
            "base_url": f"https://{role}-{position}.invalid/v1",
            "sdk_max_retries": 0,
            "request_timeout_seconds": 0.3,
        }
        for role in ("member", "chair", "review")
        for position in ("primary", "backup")
    }
    data["members"] = [{"id": "m1", "backend": "member-primary", "priorities": ["Review safely"]}]
    data["chairman"] = {"enabled": True, "backend": "chair-primary", "allow_quorum": False}
    data["code_review"] = {"enabled": True, "backend": "review-primary"}
    data["llm_fallbacks"] = {
        f"{role}-primary": [f"{role}-backup"] for role in ("member", "chair", "review")
    }
    path = tmp_path / "qa-service.json"
    path.write_text(json.dumps(data))
    service = CouncilService(load_config(str(path)))
    telemetry = []
    service._telemetry.publish_later = telemetry.append
    values = dict(
        agent_id="qa-agent",
        description="Inspect bounded workspace state",
        command="/bin/ls /workspace",
        working_directory="/workspace",
        target_resource="/workspace",
        justification="Offline full fallback acceptance",
        packaged_files=request.packaged_files,
    )
    try:
        if tool == "propose":
            result = await service.propose_intent(ProposeIntentInput(**values))
            outcome = result.state
        else:
            result = await service.authorize(AuthorizeInput(**values))
            outcome = result.status
        expected = "approved" if mode == "approve" else "denied"
        assert outcome == expected, f"F16 {tool=} {mode=} expected {expected}, result={result}"
        for role in ("member", "chair", "review"):
            assert wire.count(f"{role}-primary") == wire.count(f"{role}-backup") == 1, (
                f"F16 {tool=} {mode=} complete {role} review, one request per endpoint"
            )
        assert result.chairman.threshold == "unanimous", f"F16 {tool=} strict chairman unchanged"
        actors = telemetry[-1]["actors"]
        model_actors = [a for a in actors if a.get("backend", "").endswith("-backup")]
        assert len(model_actors) >= 3, f"F13 {tool=} {mode=} member/chair/review actor attribution"
        for actor in model_actors:
            assert actor["provider"] == "openai", f"F13 {tool=} actual provider"
            assert actor["configured_model"].endswith("-backup"), f"F13 {tool=} configured model"
            assert len(actor["llm_attempts"]) == 2, f"F13 {tool=} per-call two attempts"
        entries = [service.council.ledger.get(i) for i in range(service.council.ledger.length())]
        reviews = [
            e for e in entries if e is not None and e.entry_type.value == "intent_review_recorded"
        ]
        assert len(reviews) == 1, f"F16 {tool=} {mode=} one consolidated audit record"
        ledger_actors = reviews[0].payload["timing"]["actors"]
        assert any(a.get("backend") == "member-backup" for a in ledger_actors), (
            "F13 ledger attribution"
        )
        assert "offline-key" not in json.dumps(ledger_actors), "F13 no credential telemetry"
    finally:
        await close_clients(clients)


@pytest.mark.parametrize("measured,reject", [(None, True), (1.0, False), (10.0, True)])
def test_singleton_overload_preserves_measured_ewma_and_cold_start(
    tmp_path, monkeypatch, measured, reject
):
    import nomos.llm.council_agent as agent_module

    class OfflineBackend(ScriptBackend):
        def __init__(self, **kwargs):
            super().__init__("openai")

    monkeypatch.setattr(agent_module, "_backend_class", lambda provider: OfflineBackend)
    data = service_data(tmp_path, routes=False)
    data["llm_backends"]["primary"]["overload_latency_threshold_seconds"] = 10
    service = CouncilService(CouncilSettings.model_validate(data))
    service._limiters[0].waiting = 100
    service._intent_latency_ewma_s = measured
    assert (service._overload_rejection() is not None) == reject, f"F11 singleton {measured=}"


def test_one_unavailable_required_route_still_sheds_with_other_healthy_route(tmp_path, monkeypatch):
    import nomos.llm.council_agent as agent_module

    class OfflineBackend(ScriptBackend):
        def __init__(self, **kwargs):
            super().__init__("openai")

    monkeypatch.setattr(agent_module, "_backend_class", lambda provider: OfflineBackend)
    data = service_data(tmp_path)
    data["llm_fallbacks"] = {"primary": ["backup"]}
    data["members"].append({"id": "m2", "backend": "second", "priorities": ["Review"]})
    for name, cfg in data["llm_backends"].items():
        cfg.update(
            base_url=f"https://{name}.invalid/v1",
            max_concurrent=1,
            overload_latency_threshold_seconds=10,
        )
    service = CouncilService(CouncilSettings.model_validate(data))
    route = service._create_backend_candidates("primary")
    single = service._create_backend_candidates("second")
    for entry in route:
        entry.backend.limiter.waiting = 0
    single[0].backend.limiter.waiting = 100
    assert service._overload_rejection() is not None, (
        "F11 required singleton unavailable still sheds"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("lookalike", ["BusinessTimeoutError", "status429"])
async def test_unknown_error_cannot_become_availability_via_legacy_normalization(lookalike):
    class BusinessTimeoutError(Exception):
        pass

    class UnknownStatusError(Exception):
        status_code = 429

    error = (
        BusinessTimeoutError("unknown business failure")
        if lookalike == "BusinessTimeoutError"
        else UnknownStatusError("unknown status source")
    )
    raw = ScriptBackend("primary", [error])
    primary = LimitedBackend(raw, limiter_module.EndpointLimiter(1))
    backup = ScriptBackend("backup")
    result = await member(primary, candidates(primary, backup)).evaluate_intent(intent())
    assert result.decision == "deny", f"F06 {lookalike=} must fail closed"
    assert len(raw.calls) == 1 and backup.calls == [], (
        f"F06 {lookalike=} zero fallback after wrapper"
    )
