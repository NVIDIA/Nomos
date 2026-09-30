# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Hermes adapter tests independent of a running Hermes installation."""

import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from nomos_client import AuthorizeDecision, ConsumeDecision
from nomos_hermes.config import HermesNomosConfig, _load_public_keys
from nomos_hermes.operations import UnsupportedOperationError, extract_terminal_operation
from nomos_hermes.plugin import HermesAuthorizationGate, _build_gate, _verify_hermes_hook_safety
from trusted_executor_contract import WritOfExecution


class FakeClient:
    def __init__(self, authorize, consume):
        self.authorize_result = authorize
        self.consume_result = consume
        self.authorize_requests = []
        self.consume_requests = []

    async def authorize(self, request):
        self.authorize_requests.append(request)
        if isinstance(self.authorize_result, BaseException):
            raise self.authorize_result
        return self.authorize_result

    async def consume(self, request):
        self.consume_requests.append(request)
        return self.consume_result


async def _run_pre(gate, tool_name, args, **kwargs):
    return await asyncio.to_thread(gate.pre_tool_call, tool_name, args, **kwargs)


def _approved_gate(*, command="echo approved", workdir="/workspace"):
    now = time.time()
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    writ = WritOfExecution(
        writ_id="writ-1",
        intent_hash="ab" * 32,
        command=command,
        working_directory=workdir,
        target_agent_id="agent-1",
        issued_at=now - 1,
        expires_at=now + 60,
        max_executions=1,
        nonce="cd" * 32,
        council_id="council-1",
    )
    writ.signature = private_key.sign(writ.canonical_bytes()).hex()
    blob = base64.b64encode(writ.model_dump_json().encode()).decode()
    client = FakeClient(
        AuthorizeDecision(
            status="approved",
            council_id="council-1",
            writ=blob,
            writ_id="writ-1",
            decision_id="decision-1",
        ),
        ConsumeDecision(
            status="consumed",
            consumed=True,
            council_id="council-1",
            writ_id="writ-1",
        ),
    )
    config = HermesNomosConfig(
        enabled=True,
        mcp_url="https://nomos.example/mcp",
        auth_token="secret-token",
        agent_id="agent-1",
        council_id="council-1",
        council_public_keys=(public_key,),
    )
    gate = HermesAuthorizationGate(config, client, hook_safety_check=lambda *_: None)
    return gate, client


@pytest.mark.asyncio
async def test_approval_consumes_writ_and_preserves_exact_arguments(caplog):
    caplog.set_level("INFO", logger="nomos.hermes")
    gate, client = _approved_gate()
    args = {"command": "echo approved", "workdir": "/workspace"}

    result = await _run_pre(
        gate,
        "terminal",
        args,
        task_id="task-1",
        session_id="session-1",
        tool_call_id="call-1",
        turn_id="turn-1",
        api_request_id="request-1",
    )

    assert result is None
    assert args == {"command": "echo approved", "workdir": "/workspace"}
    assert client.authorize_requests[0].command == "echo approved"
    assert client.authorize_requests[0].context == ""
    serialized_request = client.authorize_requests[0].model_dump_json()
    assert "task-1" not in serialized_request
    assert "session-1" not in serialized_request
    assert "turn-1" not in serialized_request
    assert "request-1" not in serialized_request
    assert client.consume_requests[0].writ_id == "writ-1"

    gate.post_tool_call(
        "terminal",
        args,
        session_id="session-1",
        tool_call_id="call-1",
        status="success",
        duration_ms=12,
        result="sensitive output",
    )
    assert "writ_id=writ-1" in caplog.text
    assert "decision_id=decision-1" in caplog.text
    assert "status=success" in caplog.text
    assert "duration_ms=12" in caplog.text
    assert "arguments_matched_authorization=True" in caplog.text
    assert "intent_hash" not in caplog.text
    assert "key_fingerprint" not in caplog.text
    assert "sensitive output" not in caplog.text
    assert "echo approved" not in caplog.text
    assert "secret-token" not in caplog.text


@pytest.mark.asyncio
async def test_correlated_api_context_reaches_live_authorization_and_is_reusable():
    gate, client = _approved_gate()
    gate.pre_api_request(
        request_messages=[{"role": "user", "content": "review this operation"}],
        user_message="review this operation",
        task_id="task-1",
        session_id="session-1",
        turn_id="turn-1",
        api_request_id="request-1",
        retry_count=0,
    )

    for call_id in ("call-1", "call-2"):
        result = await _run_pre(
            gate,
            "terminal",
            {"command": "echo approved", "workdir": "/workspace"},
            task_id="task-1",
            session_id="session-1",
            tool_call_id=call_id,
            turn_id="turn-1",
            api_request_id="request-1",
        )
        assert result is None

    assert len(client.authorize_requests) == 2
    for request in client.authorize_requests:
        payload = json.loads(request.context)
        assert payload["messages"][0]["text"] == "review this operation"


@pytest.mark.asyncio
async def test_missing_or_mismatched_context_does_not_block_authorization():
    gate, client = _approved_gate()
    gate.pre_api_request(
        request_messages=[{"role": "user", "content": "do not reuse me"}],
        user_message="do not reuse me",
        task_id="task-1",
        session_id="session-1",
        turn_id="turn-1",
        api_request_id="request-1",
    )

    result = await _run_pre(
        gate,
        "terminal",
        {"command": "echo approved", "workdir": "/workspace"},
        task_id="different-task",
        session_id="session-1",
        tool_call_id="call-1",
        turn_id="turn-1",
        api_request_id="request-1",
    )

    assert result is None
    assert client.authorize_requests[0].context == ""


@pytest.mark.asyncio
async def test_context_capture_failure_is_metadata_only_and_authorization_continues(
    monkeypatch, caplog
):
    gate, client = _approved_gate()
    caplog.set_level("WARNING", logger="nomos.hermes")
    gate.pre_api_request(
        request_messages=[{"role": "user", "content": "STALE-RETRY-ZERO"}],
        user_message="STALE-RETRY-ZERO",
        task_id="task-1",
        session_id="session-1",
        turn_id="turn-1",
        api_request_id="request-1",
        retry_count=0,
    )

    def fail_capture(*_, **__):
        raise RuntimeError("PRIVATE-CAPTURE-DETAIL")

    monkeypatch.setattr("nomos_hermes.plugin.build_review_context", fail_capture)
    gate.pre_api_request(
        request_messages=[{"role": "user", "content": "PRIVATE-PROMPT"}],
        user_message="PRIVATE-PROMPT",
        task_id="task-1",
        session_id="session-1",
        turn_id="turn-1",
        api_request_id="request-1",
        retry_count=1,
    )
    result = await _run_pre(
        gate,
        "terminal",
        {"command": "echo approved", "workdir": "/workspace"},
        task_id="task-1",
        session_id="session-1",
        tool_call_id="call-1",
        turn_id="turn-1",
        api_request_id="request-1",
    )

    assert result is None
    assert client.authorize_requests[0].context == ""
    assert "reason=capture_error" in caplog.text
    assert "PRIVATE-CAPTURE-DETAIL" not in caplog.text
    assert "PRIVATE-PROMPT" not in caplog.text
    assert "STALE-RETRY-ZERO" not in client.authorize_requests[0].context


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {"command": "echo approved"},
        {"command": "echo approved", "workdir": "relative"},
        {"command": "echo approved", "workdir": "/workspace", "background": True},
        {"command": "echo approved", "workdir": "/workspace", "timeout": 10},
    ],
)
async def test_unsupported_terminal_shapes_fail_closed(args):
    gate, client = _approved_gate()
    result = await _run_pre(
        gate,
        "terminal",
        args,
        session_id="session-1",
        tool_call_id="call-1",
    )
    assert result["action"] == "block"
    assert not client.authorize_requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name",
    [
        "browser_exec",
        "cronjob_manage",
        "execute_code",
        "patch",
        "process",
        "process_manage",
        "setup_mcp",
        "skill_manage",
        "write_file",
    ],
)
async def test_alternate_local_mutation_tools_are_blocked(tool_name):
    gate, client = _approved_gate()
    result = await _run_pre(
        gate,
        tool_name,
        {},
        session_id="session-1",
        tool_call_id="call-1",
    )
    assert result["action"] == "block"
    assert not client.authorize_requests


@pytest.mark.asyncio
async def test_denial_transport_failure_bad_writ_and_replay_all_block():
    gate, client = _approved_gate()
    args = {"command": "echo approved", "workdir": "/workspace"}

    client.authorize_result = AuthorizeDecision(status="denied")
    assert (await _run_pre(gate, "terminal", args, session_id="s", tool_call_id="deny"))[
        "action"
    ] == "block"

    client.authorize_result = RuntimeError("secret transport detail")
    result = await _run_pre(gate, "terminal", args, session_id="s", tool_call_id="error")
    assert result == {
        "action": "block",
        "message": "BLOCKED by Nomos: authorization validation failed",
    }

    client.authorize_result = AuthorizeDecision(status="approved", writ="not-base64")
    assert (await _run_pre(gate, "terminal", args, session_id="s", tool_call_id="bad-writ"))[
        "action"
    ] == "block"

    approved_gate, approved_client = _approved_gate()
    approved_client.consume_result = ConsumeDecision(
        status="already_used",
        consumed=False,
        council_id="council-1",
        writ_id="writ-1",
    )
    assert (await _run_pre(approved_gate, "terminal", args, session_id="s", tool_call_id="replay"))[
        "action"
    ] == "block"


@pytest.mark.asyncio
async def test_end_to_end_timeout_blocks_without_late_consume_or_pending_state():
    class DelayedClient:
        def __init__(self):
            self.consume_requests = []

        async def authorize(self, _):
            await asyncio.sleep(0.2)
            return AuthorizeDecision(status="denied")

        async def consume(self, request):
            self.consume_requests.append(request)
            raise AssertionError("consume must not run after authorization timeout")

    gate, _ = _approved_gate()
    client = DelayedClient()
    gate._client = client
    gate._config = HermesNomosConfig(
        enabled=True,
        mcp_url="https://nomos.example/mcp",
        auth_token="secret-token",
        agent_id="agent-1",
        council_id="council-1",
        council_public_keys=gate._config.council_public_keys,
        timeout_seconds=0.01,
    )

    result = await _run_pre(
        gate,
        "terminal",
        {"command": "echo approved", "workdir": "/workspace"},
        session_id="session-1",
        tool_call_id="timeout-call",
    )
    await asyncio.sleep(0.25)

    assert result["action"] == "block"
    assert not client.consume_requests
    assert gate._pending.pop(("session-1", "timeout-call")) is None


def test_operation_extractor_passes_unrelated_typed_tools():
    assert extract_terminal_operation("jira_search", {"query": "NOMOS"}) is None
    with pytest.raises(UnsupportedOperationError):
        extract_terminal_operation("execute_code", {"code": "print(1)"})


@pytest.mark.asyncio
async def test_competing_pre_tool_hook_fails_closed_before_authorization():
    def reject_competing_hook(*_):
        raise RuntimeError("competing hook")

    gate, client = _approved_gate()
    gate._hook_safety_check = reject_competing_hook

    result = await _run_pre(
        gate,
        "terminal",
        {"command": "echo approved", "workdir": "/workspace"},
        session_id="session-1",
        tool_call_id="call-1",
    )

    assert result == {
        "action": "block",
        "message": "BLOCKED by Nomos: authorization validation failed",
    }
    assert not client.authorize_requests


def test_enabled_config_removes_tokens_from_subprocess_environment(monkeypatch, tmp_path):
    key_file = tmp_path / "keys"
    key_file.write_text("11" * 32 + "\n", encoding="utf-8")
    key_file.chmod(0o444)
    values = {
        "NOMOS_HERMES_ENABLED": "true",
        "NOMOS_MCP_URL": "https://nomos.example/mcp",
        "NOMOS_AUTH_TOKEN": "agent-secret",
        "NOMOS_MCP_BEARER_TOKEN": "transport-secret",
        "NOMOS_AGENT_ID": "agent-1",
        "NOMOS_COUNCIL_ID": "council-1",
        "NOMOS_COUNCIL_PUBLIC_KEYS_FILE": str(key_file),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    config = HermesNomosConfig.from_env()

    assert config.auth_token == "agent-secret"
    assert config.transport_token == "transport-secret"
    assert "NOMOS_AUTH_TOKEN" not in os.environ
    assert "NOMOS_MCP_BEARER_TOKEN" not in os.environ
    assert "agent-secret" not in repr(config)
    assert "transport-secret" not in repr(config)


def test_misspelled_enablement_fails_closed_and_scrubs_tokens(monkeypatch):
    monkeypatch.setenv("NOMOS_HERMES_ENABLED", "ture")
    monkeypatch.setenv("NOMOS_AUTH_TOKEN", "agent-secret")
    monkeypatch.setenv("NOMOS_MCP_BEARER_TOKEN", "transport-secret")

    with pytest.raises(ValueError, match="explicit boolean"):
        HermesNomosConfig.from_env()

    assert "NOMOS_AUTH_TOKEN" not in os.environ
    assert "NOMOS_MCP_BEARER_TOKEN" not in os.environ


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("NOMOS_HERMES_TIMEOUT_SECONDS", "nan"),
        ("NOMOS_HERMES_MAX_WRIT_LIFETIME_SECONDS", "inf"),
        ("NOMOS_HERMES_CLOCK_SKEW_SECONDS", "nan"),
    ],
)
def test_enabled_config_rejects_non_finite_time_bounds(monkeypatch, name, value):
    monkeypatch.setenv("NOMOS_HERMES_ENABLED", "true")
    monkeypatch.setenv("NOMOS_AUTH_TOKEN", "agent-secret")
    monkeypatch.setenv("NOMOS_MCP_BEARER_TOKEN", "transport-secret")
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match="time bounds"):
        HermesNomosConfig.from_env()
    assert "NOMOS_AUTH_TOKEN" not in os.environ
    assert "NOMOS_MCP_BEARER_TOKEN" not in os.environ


def test_enabled_config_rejects_insecure_or_credentialed_mcp_url(monkeypatch, tmp_path):
    key_file = tmp_path / "keys"
    key_file.write_text("11" * 32 + "\n", encoding="utf-8")
    key_file.chmod(0o444)
    base = {
        "NOMOS_HERMES_ENABLED": "true",
        "NOMOS_AUTH_TOKEN": "agent-secret",
        "NOMOS_MCP_BEARER_TOKEN": "transport-secret",
        "NOMOS_AGENT_ID": "agent-1",
        "NOMOS_COUNCIL_ID": "council-1",
        "NOMOS_COUNCIL_PUBLIC_KEYS_FILE": str(key_file),
    }
    for url in ("http://nomos.example/mcp", "https://user:pass@nomos.example/mcp"):
        for name, setting in base.items():
            monkeypatch.setenv(name, setting)
        monkeypatch.setenv("NOMOS_MCP_URL", url)
        with pytest.raises(ValueError, match="NOMOS_MCP_URL"):
            HermesNomosConfig.from_env()
        assert "NOMOS_AUTH_TOKEN" not in os.environ
        assert "NOMOS_MCP_BEARER_TOKEN" not in os.environ


def test_public_key_loader_accepts_kubernetes_projected_symlink(tmp_path):
    revision = tmp_path / "..2026_09_29"
    revision.mkdir()
    target = revision / "keys"
    target.write_text("22" * 32 + "\n", encoding="utf-8")
    target.chmod(0o444)
    (tmp_path / "..data").symlink_to(revision.name)
    projected = tmp_path / "keys"
    projected.symlink_to(Path("..data") / "keys")

    assert _load_public_keys(str(projected)) == (bytes.fromhex("22" * 32),)


def test_public_key_loader_rejects_symlink_outside_mount(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-key"
    outside.write_text("33" * 32 + "\n", encoding="utf-8")
    outside.chmod(0o444)
    projected = tmp_path / "keys"
    projected.symlink_to(outside)
    try:
        with pytest.raises(ValueError, match="escapes"):
            _load_public_keys(str(projected))
    finally:
        outside.unlink()


def test_enabled_plugin_rejects_unreviewed_hermes_version(monkeypatch):
    config = HermesNomosConfig(enabled=True)
    monkeypatch.setattr(HermesNomosConfig, "from_env", classmethod(lambda cls: config))
    monkeypatch.setattr("nomos_hermes.plugin.version", lambda _: "0.21.4")

    with pytest.raises(ValueError, match="unsupported"):
        _build_gate()


def test_hook_safety_uses_hermes_effective_timeout_clamp(monkeypatch):
    def callback(**_):
        return None

    def load_config_readonly():
        return {"plugins": {"hook_callback_timeout": 1000}}

    def iter_hook_callbacks(_):
        return (callback,)

    config_module = ModuleType("hermes_cli.config")
    config_module.load_config_readonly = load_config_readonly
    plugins_module = ModuleType("hermes_cli.plugins")
    plugins_module.iter_hook_callbacks = iter_hook_callbacks
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config_module)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins_module)

    with pytest.raises(RuntimeError, match="must exceed"):
        _verify_hermes_hook_safety(callback, 700)


def test_hook_safety_rejects_non_finite_timeout(monkeypatch):
    def callback(**_):
        return None

    config_module = ModuleType("hermes_cli.config")
    config_module.load_config_readonly = lambda: {
        "plugins": {"hook_callback_timeout": float("nan")}
    }
    plugins_module = ModuleType("hermes_cli.plugins")
    plugins_module.iter_hook_callbacks = lambda _: (callback,)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config_module)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins_module)

    with pytest.raises(RuntimeError, match="finite"):
        _verify_hermes_hook_safety(callback, 10)
