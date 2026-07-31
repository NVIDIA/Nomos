# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for trusted executor HTTP API and agent binding checks."""

import asyncio
import base64
import hashlib
import json
import threading
import time
import urllib.request

import pytest

from executors.common import load_or_create_executor_identity
from executors.docker.api import _validate_agent_binding, create_server
from executors.docker.config import ExecutorConfig
from executors.docker.models import ExecutionContext, WritOfExecution
from trusted_executor_contract import (
    CONTRACT_VERSION,
    EXECUTE_ENDPOINT,
    HEALTH_ENDPOINT,
    WELL_KNOWN_ENDPOINT,
    ExecutionEvidence,
    ExecutionResult,
)


def _writ_blob(target_agent_id: str = "agent-1") -> str:
    now = time.time()
    writ = WritOfExecution(
        writ_id="writ-1",
        intent_hash="ab" * 32,
        command="echo hello",
        working_directory="/tmp",
        target_agent_id=target_agent_id,
        issued_at=now - 5,
        expires_at=now + 60,
        max_executions=1,
        nonce="cd" * 32,
        council_id="council-test",
        threshold_used="quorum",
        chairman_risk_level="low",
    )
    return base64.b64encode(writ.model_dump_json().encode("utf-8")).decode("utf-8")


def test_missing_agent_header_returns_agent_binding_error():
    err = _validate_agent_binding(_writ_blob(), caller_agent_id="")

    assert err is not None
    assert err["success"] is False
    assert err["verification_stage"] == "agent_binding"
    assert "X-Agent-ID" in err["error"]


def test_invalid_writ_payload_returns_agent_binding_error():
    err = _validate_agent_binding("not-valid-base64", caller_agent_id="agent-1")

    assert err is not None
    assert err["success"] is False
    assert err["verification_stage"] == "agent_binding"


def test_agent_binding_mismatch_returns_agent_binding_error_with_writ_id():
    err = _validate_agent_binding(_writ_blob(target_agent_id="agent-1"), caller_agent_id="agent-2")

    assert err is not None
    assert err["writ_id"] == "writ-1"
    assert err["success"] is False
    assert err["verification_stage"] == "agent_binding"
    assert "mismatch" in err["error"].lower()


def test_agent_binding_match_passes():
    err = _validate_agent_binding(_writ_blob(target_agent_id="agent-1"), caller_agent_id="agent-1")

    assert err is None


class _FakeEnforcer:
    async def execute_writ(
        self,
        serialized_writ: str,
        context: ExecutionContext | None = None,
    ) -> ExecutionResult:
        return ExecutionResult(
            writ_id="writ-1",
            success=True,
            exit_code=0,
            stdout=f"caller={context.caller_agent_id if context else ''}",
            verification_stage="passed",
            executed_command_shell=False,
            executed_command_argv=["echo", "hello"],
        )


@pytest.fixture
def executor_server():
    loop = asyncio.new_event_loop()
    server = create_server(
        ExecutorConfig(listen_host="127.0.0.1", listen_port=0),
        _FakeEnforcer(),
        b"\x01" * 32,
        loop=loop,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        loop.close()


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310
        return json.loads(resp.read())


def _post_json(url: str, body: dict, headers: dict[str, str] | None = None) -> dict:
    req = urllib.request.Request(  # noqa: S310
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
        return json.loads(resp.read())


def test_versioned_health_endpoint_advertises_contract(executor_server):
    data = _get_json(f"{executor_server}{HEALTH_ENDPOINT}")

    assert data["status"] == "ok"
    assert data["contract_version"] == CONTRACT_VERSION
    assert data["executor_id"].startswith("example-local-subprocess:")
    assert data["capabilities"]["execution_modes"] == ["local-subprocess"]


def test_well_known_endpoint_describes_executor_contract(executor_server):
    data = _get_json(f"{executor_server}{WELL_KNOWN_ENDPOINT}")

    assert data["name"] == "nomos.trusted-executor"
    assert data["contract_version"] == CONTRACT_VERSION
    assert data["execute_endpoint"] == EXECUTE_ENDPOINT
    assert data["agent_id_header"] == "X-Agent-ID"


def test_versioned_execute_endpoint_uses_contract_adapter(executor_server):
    data = _post_json(
        f"{executor_server}{EXECUTE_ENDPOINT}",
        {"writ": _writ_blob(target_agent_id="agent-1")},
        {"X-Agent-ID": "agent-1"},
    )

    assert data["contract_version"] == CONTRACT_VERSION
    assert data["executor_id"].startswith("example-local-subprocess:")
    assert data["success"] is True
    assert data["verification_stage"] == "passed"
    assert data["stdout"] == "caller=agent-1"


def test_executor_identity_generates_and_reloads_keypair(tmp_path):
    private_key = tmp_path / "executor.key"
    public_key = tmp_path / "executor.pub"

    first = load_or_create_executor_identity(str(private_key), str(public_key))
    second = load_or_create_executor_identity(str(private_key), str(public_key))

    assert private_key.exists()
    assert public_key.read_bytes() == first.public_key_bytes
    assert second.key_fingerprint == first.key_fingerprint


def test_execute_response_includes_verifiable_executor_evidence():
    loop = asyncio.new_event_loop()
    identity = load_or_create_executor_identity()
    server = create_server(
        ExecutorConfig(listen_host="127.0.0.1", listen_port=0),
        _FakeEnforcer(),
        b"\x01" * 32,
        loop=loop,
        executor_identity=identity,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        data = _post_json(
            f"{base_url}{EXECUTE_ENDPOINT}",
            {"writ": _writ_blob(target_agent_id="agent-1"), "request_id": "req-1"},
            {"X-Agent-ID": "agent-1"},
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        loop.close()

    evidence = ExecutionEvidence.model_validate(data["evidence"])
    assert evidence.verify() is True
    assert data["executor_id"] == f"example-local-subprocess:{identity.key_fingerprint[:16]}"
    assert evidence.payload.executor_id == data["executor_id"]
    assert evidence.key_fingerprint == identity.key_fingerprint
    assert evidence.payload.request_id == "req-1"
    assert evidence.payload.command.command == "echo hello"
    assert evidence.payload.command.shell is False
    assert evidence.payload.command.argv == ["echo", "hello"]
    assert (
        evidence.payload.result.stdout_sha256 == hashlib.sha256(data["stdout"].encode()).hexdigest()
    )
    assert evidence.payload.result.verification_stage == "passed"
