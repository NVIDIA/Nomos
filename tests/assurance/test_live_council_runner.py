# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Live validation must never dispatch an unapproved or mismatched writ."""

import base64
import hashlib
import json
import time

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from executors.common.evidence import ExecutorIdentity, certify_execution_result
from nomos.level_0.crypto import KeyPair, Signer
from nomos.level_1.ledger.chain import LedgerChain
from nomos.level_1.ledger.entry import LedgerEntryType
from scripts.assurance_live_council_tests import (
    Config,
    check_observer,
    run,
    validate_receipt,
    validate_writ,
)
from trusted_executor_contract import ExecutionResult, WritOfExecution


@pytest.fixture
def fixture():
    council = KeyPair.generate("test-council")
    executor_key = Ed25519PrivateKey.generate()
    executor = ExecutorIdentity(
        executor_key, executor_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    )
    config = Config(
        mcp_url="http://council/mcp",
        executor_url="http://executor",
        agent_id="test-agent",
        auth_token="test-agent-token",
        proxy_token="test-proxy-token",
        council_id="council-test",
        working_directory="/workspace",
        council_key_hex=council.public_key_hex(),
        executor_keys={"test-executor": [executor.public_key_hex]},
    )
    return config, council, executor


def signed_writ(config, council, command="/usr/bin/false", writ_id="writ-test"):
    writ = WritOfExecution(
        writ_id=writ_id,
        intent_hash="intent-hash",
        target_agent_id=config.agent_id,
        command=command,
        working_directory=config.working_directory,
        issued_at=time.time() - 1,
        expires_at=time.time() + 300,
        nonce="01" * 32,
        council_id=config.council_id,
    )
    writ.signature = Signer(council).sign(writ.canonical_bytes()).signature_bytes.hex()
    return writ, base64.b64encode(writ.model_dump_json().encode()).decode()


@pytest.mark.parametrize(
    "field,value",
    [
        ("command", "/usr/bin/touch /workspace/forbidden"),
        ("target_agent_id", "other"),
        ("council_id", "other"),
        ("working_directory", "/app"),
        ("expires_at", 1),
    ],
)
def test_signed_but_mismatched_writ_never_passes_validation(fixture, field, value):
    config, council, _ = fixture
    writ, _ = signed_writ(config, council)
    setattr(writ, field, value)
    writ.signature = Signer(council).sign(writ.canonical_bytes()).signature_bytes.hex()
    encoded = base64.b64encode(writ.model_dump_json().encode()).decode()
    with pytest.raises(ValueError):
        validate_writ(
            config,
            {"status": "approved", "writ": encoded, "writ_id": writ.writ_id},
            "/usr/bin/false",
        )


@pytest.mark.parametrize("tamper", ["signature", "stdout", "agent", "command", "pin"])
def test_receipt_must_bind_the_pinned_executor_writ_and_output(fixture, tamper):
    config, council, executor = fixture
    writ, encoded = signed_writ(config, council)
    result = certify_execution_result(
        encoded,
        ExecutionResult(
            executor_id="test-executor",
            writ_id=writ.writ_id,
            success=False,
            exit_code=1,
            stdout="",
            stderr="",
            verification_stage="passed",
        ),
        executor,
        request_id="run:failure",
    )
    if tamper == "signature":
        result.evidence.signature = "00" * 64
    elif tamper == "stdout":
        result.stdout = "forged"
    elif tamper == "agent":
        result.evidence.payload.target_agent_id = "other"
        result.evidence.signature = executor.sign(result.evidence.payload.canonical_bytes()).hex()
    elif tamper == "command":
        result.evidence.payload.command.command = "/usr/bin/true"
        result.evidence.signature = executor.sign(result.evidence.payload.canonical_bytes()).hex()
    else:
        config.executor_keys = {"test-executor": ["00" * 32]}
    with pytest.raises(ValueError):
        validate_receipt(config, result.model_dump(), writ, "run:failure", 1)


@pytest.mark.asyncio
async def test_denied_authorization_prevents_executor_dispatch_and_secrets_are_redacted(fixture):
    config, council, _ = fixture
    chain = LedgerChain(signer=Signer(council), chain_id="ledger-council-test")
    chain.initialize("test")
    dispatches = []

    async def transport(request):
        assert request.url.host == "council"
        data = json.loads(request.content)
        if data["method"] == "initialize":
            result = {"protocolVersion": "2025-03-26"}
        elif data["method"] == "notifications/initialized":
            return httpx.Response(202)
        else:
            params = data["params"]
            dispatches.append(params["name"])
            if params["name"] == "council_ledger_page":
                body = {
                    "chain_id": chain.chain_id,
                    "head_sequence": 0,
                    "entries": [chain.get(0).to_dict()],
                }
            elif params["name"] == "council_propose_intent":
                body = {"success": True, "state": "denied", "intent_id": str(len(dispatches))}
            else:
                body = {"status": "denied", "error": "test-agent-token test-proxy-token"}
            result = {"content": [{"type": "text", "text": json.dumps(body)}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": data["id"], "result": result})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        report = await run(config, "test-run", client=client)
    assert report["passed"] is False
    assert report["error_type"] == "ValueError"
    assert dispatches.count("council_propose_intent") == 3
    assert dispatches.count("council_authorize") == 1
    assert "test-agent-token" not in json.dumps(report)
    assert "test-proxy-token" not in json.dumps(report)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [None, "denial", "failure"])
@pytest.mark.parametrize("delayed", [False, True])
async def test_observer_requires_both_real_findings_bound_to_new_review_sequences(
    missing, delayed, monkeypatch
):
    from types import SimpleNamespace

    from scripts import assurance_live_council_tests as runner

    clock = {"now": 0.0}
    if delayed:

        async def sleep(duration):
            clock["now"] += duration

        # Keep the actual event-loop timer intact; advance only the runner clock.
        monkeypatch.setattr(runner, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
        monkeypatch.setattr(runner.asyncio, "sleep", sleep)
    report = {
        "council_id": "c",
        "agent_id": "agent",
        "final_head_sequence": 4,
        "reviews": [
            *[{"kind": "policy_denial", "sequence": n} for n in (1, 2, 3)],
            {"kind": "certified_failure_review", "sequence": 4},
        ],
    }
    findings = [
        {
            "id": "denial",
            "kind": "repeated_unchanged_denial",
            "agent_id": "agent",
            "synthetic": False,
            "evidence": [{"chain_id": "ledger-c", "sequence": n} for n in (1, 2, 3)],
        },
        {
            "id": "failure",
            "kind": "explicit_tool_failure",
            "agent_id": "agent",
            "synthetic": False,
            "evidence": [{"chain_id": "ledger-c", "sequence": 4}],
        },
    ]

    async def transport(request):
        if request.url.path == "/api/status":
            return httpx.Response(
                200,
                json={
                    "chain_id": "ledger-c",
                    "checkpoint": {"sequence": 4},
                    "analysis_sequence": 4 if not delayed or clock["now"] >= 300 else 0,
                    "analysis_seconds": 300,
                    "worker": {"state": "ok"},
                },
            )
        return httpx.Response(200, json={"findings": [f for f in findings if f["id"] != missing]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        if missing:
            with pytest.raises(ValueError):
                await check_observer(
                    report, client=client, url="http://observer", auth=("reader", "password")
                )
        else:
            await check_observer(
                report, client=client, url="http://observer", auth=("reader", "password")
            )
            assert report["observer_checked"] is True
            assert {f["id"] for f in report["observer"]["findings"]} == {"denial", "failure"}
        if delayed:
            assert clock["now"] == 300


@pytest.mark.asyncio
async def test_runner_uses_real_reviews_execution_receipt_and_signed_ledger_readback(fixture):
    config, council, executor = fixture
    chain = LedgerChain(signer=Signer(council), chain_id="ledger-council-test")
    chain.initialize("test")
    executed = []
    seen_receipts = []
    reviews = 0

    async def transport(request):
        nonlocal reviews
        data = json.loads(request.content)
        if request.url.host == "executor":
            assert "authorization" not in request.headers
            assert request.headers["X-Agent-ID"] == config.agent_id
            assert request.url.path == "/execute"
            writ = WritOfExecution.model_validate_json(base64.b64decode(data["writ"]))
            executed.append(writ.command)
            code = 0 if writ.command == "/usr/bin/true" else 1
            receipt = certify_execution_result(
                data["writ"],
                ExecutionResult(
                    executor_id="test-executor",
                    writ_id=writ.writ_id,
                    success=code == 0,
                    exit_code=code,
                    stdout="",
                    stderr="",
                    verification_stage="passed",
                ),
                executor,
                request_id=data["request_id"],
            )
            return httpx.Response(200, json=receipt.model_dump())
        assert request.headers["Authorization"] == "Bearer test-proxy-token"
        if data["method"] == "initialize":
            result = {"protocolVersion": "2025-03-26"}
        elif data["method"] == "notifications/initialized":
            return httpx.Response(202)
        else:
            params = data["params"]
            args = params["arguments"]
            if params["name"] == "council_ledger_page":
                body = {
                    "chain_id": chain.chain_id,
                    "head_sequence": chain.length() - 1,
                    "entries": [chain.get(i).to_dict() for i in range(chain.length() - 1, -1, -1)],
                }
            else:
                assert args["auth_token"] == config.auth_token
                assert args["command"] in {"true", "/usr/bin/true", "/usr/bin/false"}
                reviews += 1
                intent_id = f"intent-{reviews}"
                denied = args["command"] == "true"
                evidence = args.get("certified_evidence", [])
                seen_receipts.extend(evidence)
                payload = {
                    "agent_id": config.agent_id,
                    "intent_id": intent_id,
                    "intent": {
                        "command": args["command"],
                        "metadata": {
                            "certified_evidence": [
                                {
                                    "writ_id": item["writ_id"],
                                    "success": item["success"],
                                    "exit_code": item["exit_code"],
                                }
                                for item in evidence
                            ]
                        },
                    },
                    "opa": {"allow": not denied},
                    "final": {"state": "denied" if denied else "approved"},
                }
                chain.append(LedgerEntryType.INTENT_REVIEW_RECORDED, payload)
                if params["name"] == "council_propose_intent":
                    body = {
                        "success": True,
                        "intent_id": intent_id,
                        "state": "denied" if denied else "approved",
                    }
                else:
                    writ, encoded = signed_writ(config, council, args["command"], f"writ-{reviews}")
                    body = {
                        "status": "approved",
                        "writ": encoded,
                        "writ_id": writ.writ_id,
                        "decision_id": intent_id,
                        "council_members": [
                            {"member_id": f"member-{i}", "decision": "approve"} for i in range(3)
                        ],
                    }
            result = {"content": [{"type": "text", "text": json.dumps(body)}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": data["id"], "result": result})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        report = await run(config, "test-run", client=client)
    assert report["passed"] is True
    assert report["synthetic"] is False
    assert executed == ["/usr/bin/true", "/usr/bin/false"]
    assert len(seen_receipts) == 1
    assert seen_receipts[0]["success"] is False
    assert report["ledger_sequences"] == [1, 2, 3, 4, 5, 6]
    assert (
        report["executions"][1]["executor_key_fingerprint"]
        == hashlib.sha256(executor.public_key_bytes).hexdigest()
    )
    assert "test-agent-token" not in json.dumps(report)
    assert "test-proxy-token" not in json.dumps(report)
