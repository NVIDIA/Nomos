# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

from nomos.config.settings import CouncilSettings, GuardrailsConfig, OPAConfig
from nomos.guardrails import (
    GuardrailsEvaluationError,
    GuardrailsHttpClient,
    GuardrailsScreenDecision,
    build_screen_text,
    parse_screen_response,
)
from nomos.level_0.types import Intent, RollbackPlan
from nomos.level_4.council.workflow import WorkflowState
from nomos.nat.functions import AuthorizeInput, CouncilService, ProposeIntentInput


class RecordingGuardrailsClient:
    def __init__(
        self,
        decision: GuardrailsScreenDecision | None = None,
        error: Exception | None = None,
    ):
        self.decision = decision or GuardrailsScreenDecision(allow=True)
        self.error = error
        self.calls: list[str] = []

    async def evaluate(self, text: str) -> GuardrailsScreenDecision:
        self.calls.append(text)
        if self.error is not None:
            raise self.error
        return self.decision


def _blocked_decision(finding: str = "input rail blocked the message: injection detection"):
    return GuardrailsScreenDecision(allow=False, blocked=True, findings=[finding])


def _make_service(
    tmp_path,
    *,
    mode: str = "enforce",
    fail_closed: bool = True,
    channels: list[str] | None = None,
) -> CouncilService:
    guardrails_kwargs = {"enabled": True, "mode": mode, "fail_closed": fail_closed}
    if channels is not None:
        guardrails_kwargs["channels"] = channels
    settings = CouncilSettings(
        council_id="guardrails-test-council",
        require_auth=False,
        require_justification=True,
        llm_backends={},
        members=[],
        council_key_path=str(tmp_path),
        opa=OPAConfig(enabled=False),
        guardrails=GuardrailsConfig(**guardrails_kwargs),
    )
    return CouncilService(settings)


def _propose_input(**overrides) -> ProposeIntentInput:
    fields = {
        "agent_id": "agent-1",
        "description": "Run a harmless absolute command",
        "target_resource": "/tmp",
        "command": "/bin/echo hello",
        "working_directory": "/tmp",
        "justification": "Testing the guardrails screen",
    }
    fields.update(overrides)
    return ProposeIntentInput(**fields)


# ---------------------------------------------------------------------------
# config validation
# ---------------------------------------------------------------------------


def test_guardrails_config_defaults_are_opt_in_and_fail_closed():
    config = GuardrailsConfig()
    assert config.enabled is False
    assert config.fail_closed is True
    assert config.mode == "enforce"
    assert "description" in config.channels
    assert "command" not in config.channels


def test_guardrails_config_rejects_unknown_mode_and_channels():
    with pytest.raises(ValueError, match="mode"):
        GuardrailsConfig(mode="advisory")
    with pytest.raises(ValueError, match="not screenable"):
        GuardrailsConfig(channels=["command"])
    with pytest.raises(ValueError, match="must not be empty"):
        GuardrailsConfig(channels=["  "])


def test_guardrails_config_normalizes_url():
    config = GuardrailsConfig(url="http://guardrails:8000/")
    assert config.url == "http://guardrails:8000"
    with pytest.raises(ValueError, match="http"):
        GuardrailsConfig(url="guardrails:8000")


# ---------------------------------------------------------------------------
# build_screen_text
# ---------------------------------------------------------------------------


def _make_intent(**overrides) -> Intent:
    fields = {
        "agent_id": "agent-screen",
        "description": "Restart the ingest service",
        "target_resource": "svc/ingest",
        "command": "/usr/bin/systemctl restart ingest",
        "working_directory": "/workspace",
        "justification": "Latency alert fired twice",
        "blast_radius": "One service restart",
        "evidence": ["p99 spiked to 4s", "queue depth 12k"],
        "rollback_plan": RollbackPlan.automatic(["/usr/bin/systemctl start ingest"]),
    }
    fields.update(overrides)
    return Intent.create(**fields)


def test_build_screen_text_labels_channels_and_excludes_command():
    intent = _make_intent()
    text, channels = build_screen_text(
        intent, ["description", "justification", "evidence", "rollback_steps"]
    )
    assert "[channel: description]\nRestart the ingest service" in text
    assert "[channel: justification]\nLatency alert fired twice" in text
    assert "[channel: evidence]" in text
    assert "queue depth 12k" in text
    assert "[channel: rollback_steps]" in text
    assert "/usr/bin/systemctl restart ingest" not in text
    assert channels == ["description", "justification", "evidence", "rollback_steps"]


def test_build_screen_text_skips_empty_channels():
    intent = _make_intent(evidence=[], blast_radius="")
    text, channels = build_screen_text(intent, ["blast_radius", "evidence", "description"])
    assert channels == ["description"]
    assert "[channel: blast_radius]" not in text


# ---------------------------------------------------------------------------
# parse_screen_response
# ---------------------------------------------------------------------------


def test_parse_screen_response_reads_activated_rails_log():
    decision = parse_screen_response(
        {
            "choices": [{"message": {"role": "assistant", "content": "I can't respond."}}],
            "guardrails": {
                "config_id": "default-council",
                "log": {
                    "activated_rails": [
                        {
                            "type": "input",
                            "name": "injection detection on user input",
                            "decisions": ["execute injection_detection", "stop"],
                            "stop": True,
                            "executed_actions": [
                                {
                                    "action_name": "injection_detection",
                                    "return_value": {
                                        "decision": "block",
                                        "metadata": {"detections": ["sql_injection"]},
                                    },
                                }
                            ],
                        },
                    ]
                },
            },
        }
    )
    assert decision.blocked is True
    assert decision.allow is False
    assert "injection detection on user input" in decision.findings[0]
    assert "sql_injection" in decision.findings[0]


def test_parse_screen_response_detects_sentinel_content():
    decision = parse_screen_response(
        {"choices": [{"message": {"role": "assistant", "content": "NOMOS_SCREEN_BLOCKED: bloat"}}]}
    )
    assert decision.blocked is True
    assert "NOMOS_SCREEN_BLOCKED" in decision.findings[0]


def test_parse_screen_response_clean_pass_through():
    decision = parse_screen_response(
        {
            "choices": [{"message": {"role": "assistant", "content": "echoed back"}}],
            "guardrails": {
                "log": {
                    "activated_rails": [
                        {
                            "type": "input",
                            "name": "context bloat detection on input",
                            "decisions": ["execute context_bloat_detection"],
                            "stop": False,
                        }
                    ]
                }
            },
        }
    )
    assert decision.blocked is False
    assert decision.allow is True
    assert decision.findings == []


# ---------------------------------------------------------------------------
# stage behaviour: enforce / shadow / fail-closed / disabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_screen_block_denies_propose_before_council(tmp_path):
    service = _make_service(tmp_path)
    client = RecordingGuardrailsClient(_blocked_decision())
    service._guardrails_client = client

    result = await service.propose_intent(_propose_input())

    assert result.success is True
    assert result.state == WorkflowState.DENIED.value
    assert "Denied by guardrails screen" in result.error
    assert "injection detection" in result.error
    assert service.council.get_workflow(result.intent_id) is None
    assert "[channel: description]" in client.calls[0]
    assert "/bin/echo hello" not in client.calls[0]


@pytest.mark.asyncio
async def test_screen_block_denies_authorize_with_guidance(tmp_path):
    service = _make_service(tmp_path)
    service._guardrails_client = RecordingGuardrailsClient(
        GuardrailsScreenDecision(
            allow=False,
            blocked=True,
            findings=["input rail blocked the message: context bloat detection on input"],
            guidance=["Trim the justification to what the council needs."],
        )
    )

    result = await service.authorize(
        AuthorizeInput(
            agent_id="agent-1",
            command="/bin/echo deploy",
            working_directory="/tmp",
            description="Deploy a configuration change",
            target_resource="svc/config",
            justification="x" * 500,
            blast_radius="One service",
        )
    )

    assert result.status == "denied"
    assert result.writ is None
    assert "context bloat" in result.error
    assert result.guidance == ["Trim the justification to what the council needs."]
    assert service.council.get_workflow(result.decision_id) is None


@pytest.mark.asyncio
async def test_screen_clean_proceeds_to_council_workflow(tmp_path):
    service = _make_service(tmp_path)
    client = RecordingGuardrailsClient()
    service._guardrails_client = client

    result = await service.propose_intent(_propose_input())

    assert result.success is True
    # With no members configured the empty quorum still denies, but the intent
    # passed the screen: a council workflow exists and was voted on.
    assert service.council.get_workflow(result.intent_id) is not None
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_shadow_mode_blocked_verdict_proceeds(tmp_path):
    service = _make_service(tmp_path, mode="shadow")
    service._guardrails_client = RecordingGuardrailsClient(_blocked_decision())

    result = await service.propose_intent(_propose_input())

    assert result.success is True
    # Shadow mode never denies at the screen: the workflow was created and the
    # error string, if any, is not the screen's.
    assert service.council.get_workflow(result.intent_id) is not None
    assert "guardrails screen" not in (result.error or "")


@pytest.mark.asyncio
async def test_shadow_mode_records_blocked_verdict(tmp_path):
    service = _make_service(tmp_path, mode="shadow")
    service._guardrails_client = RecordingGuardrailsClient(_blocked_decision())

    decision = await service._evaluate_guardrails(_make_intent())

    assert decision is not None
    assert decision.allow is True
    assert decision.blocked is True
    assert decision.mode == "shadow"
    assert "injection detection" in decision.findings[0]


@pytest.mark.asyncio
async def test_fail_closed_denies_when_sidecar_unavailable(tmp_path):
    service = _make_service(tmp_path, fail_closed=True)
    service._guardrails_client = RecordingGuardrailsClient(
        error=GuardrailsEvaluationError("connection refused"),
    )

    result = await service.authorize(
        AuthorizeInput(
            agent_id="agent-1",
            command="/bin/echo deploy",
            working_directory="/tmp",
            description="Run a harmless command",
            target_resource="/tmp",
            justification="Testing guardrails fail closed",
        )
    )

    assert result.status == "denied"
    assert "Guardrails screen failed" in result.error
    assert result.writ is None


@pytest.mark.asyncio
async def test_fail_open_allows_flow_to_continue(tmp_path):
    service = _make_service(tmp_path, fail_closed=False)
    service._guardrails_client = RecordingGuardrailsClient(
        error=GuardrailsEvaluationError("connection refused"),
    )

    result = await service.propose_intent(_propose_input())

    assert result.success is True
    assert service.council.get_workflow(result.intent_id) is not None


@pytest.mark.asyncio
async def test_shadow_mode_sidecar_unavailable_proceeds(tmp_path):
    service = _make_service(tmp_path, mode="shadow", fail_closed=True)
    service._guardrails_client = RecordingGuardrailsClient(
        error=GuardrailsEvaluationError("connection refused"),
    )

    result = await service.propose_intent(_propose_input())

    assert result.success is True
    assert service.council.get_workflow(result.intent_id) is not None


@pytest.mark.asyncio
async def test_disabled_config_skips_screen(tmp_path):
    settings = CouncilSettings(
        council_id="guardrails-off-council",
        require_auth=False,
        require_justification=True,
        llm_backends={},
        members=[],
        council_key_path=str(tmp_path),
        opa=OPAConfig(enabled=False),
    )
    service = CouncilService(settings)
    assert service._guardrails_client is None

    result = await service.propose_intent(_propose_input())

    assert result.success is True
    assert service.council.get_workflow(result.intent_id) is not None


@pytest.mark.asyncio
async def test_channels_subset_limits_screened_text(tmp_path):
    service = _make_service(tmp_path, channels=["justification"])
    client = RecordingGuardrailsClient()
    service._guardrails_client = client

    await service.propose_intent(_propose_input())

    assert "[channel: justification]" in client.calls[0]
    assert "[channel: description]" not in client.calls[0]


# ---------------------------------------------------------------------------
# HTTP client (real socket, no live sidecar) — exercises the _post path and
# the pinned server contract deterministically.
# ---------------------------------------------------------------------------


class _ScriptedHandler(BaseHTTPRequestHandler):
    status = 200
    body = b"{}"
    captured: ClassVar[dict] = {}

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        type(self).captured = json.loads(raw)
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(type(self).body)

    def log_message(self, *args):  # silence test server logging
        pass


def _serve(handler_cls):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _http_client(server) -> GuardrailsHttpClient:
    host, port = server.server_address
    return GuardrailsHttpClient(
        GuardrailsConfig(enabled=True, url=f"http://{host}:{port}", config_id="unit-council")
    )


@pytest.mark.asyncio
async def test_http_client_posts_pinned_contract_and_reads_blocked_log():
    class Handler(_ScriptedHandler):
        status = 200
        body = json.dumps(
            {
                "choices": [
                    {"message": {"role": "assistant", "content": "NOMOS_SCREEN_BLOCKED: x"}}
                ],
                "guardrails": {
                    "log": {
                        "activated_rails": [
                            {
                                "name": "injection detection on user input",
                                "stop": True,
                                "executed_actions": [
                                    {
                                        "return_value": {
                                            "metadata": {"detections": ["code_injection"]}
                                        }
                                    }
                                ],
                            }
                        ]
                    }
                },
            }
        ).encode()

    server = _serve(Handler)
    try:
        decision = await _http_client(server).evaluate("[channel: justification]\nexec(payload)")
    finally:
        server.shutdown()

    # request shape matches the pinned contract
    assert Handler.captured["model"] == "screen"
    assert Handler.captured["guardrails"]["config_id"] == "unit-council"
    assert Handler.captured["guardrails"]["options"]["rails"] == ["input"]
    # response parsed to a blocked verdict with the specific detection
    assert decision.blocked is True
    assert "code_injection" in decision.findings[0]


@pytest.mark.asyncio
async def test_http_client_reads_clean_pass_through():
    class Handler(_ScriptedHandler):
        status = 200
        body = json.dumps(
            {
                "choices": [{"message": {"role": "assistant", "content": "echoed"}}],
                "guardrails": {"log": {"activated_rails": [{"name": "ok", "stop": False}]}},
            }
        ).encode()

    server = _serve(Handler)
    try:
        decision = await _http_client(server).evaluate("[channel: description]\nrestart")
    finally:
        server.shutdown()

    assert decision.blocked is False
    assert decision.allow is True


@pytest.mark.asyncio
async def test_http_client_raises_on_http_error():
    class Handler(_ScriptedHandler):
        status = 500
        body = b'{"error":"boom"}'

    server = _serve(Handler)
    try:
        with pytest.raises(GuardrailsEvaluationError, match="HTTP 500"):
            await _http_client(server).evaluate("anything")
    finally:
        server.shutdown()


@pytest.mark.asyncio
async def test_http_client_raises_on_invalid_json():
    class Handler(_ScriptedHandler):
        status = 200
        body = b"not json"

    server = _serve(Handler)
    try:
        with pytest.raises(GuardrailsEvaluationError, match="invalid JSON"):
            await _http_client(server).evaluate("anything")
    finally:
        server.shutdown()


def test_parse_screen_response_falls_back_to_messages_shape():
    decision = parse_screen_response(
        {"messages": [{"role": "assistant", "content": "NOMOS_SCREEN_BLOCKED: bloat"}]}
    )
    assert decision.blocked is True
    assert "NOMOS_SCREEN_BLOCKED" in decision.findings[0]
