# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Opt-in: the contract the plugin relies on, checked directly against operator-provided Nomos services.

Everything the plugin certifies rests on four facts about the live Council and Executor: the Council signs writs
with the pinned key; the Executor signs its results with a key in the operator's trust snapshot; the Executor runs
a writ at most once; and the Council really validates the certified evidence it is sent (so an "accepted" citation
means something). These tests check each one with the plugin's own clients and verifiers, no Relay in between.

NOMOS_TEST_LIVE_CONFIG names the operator config (as for test_live_worker.py).
"""

import copy
import hashlib
import json
import os
import uuid
from pathlib import Path

import pytest

if not os.getenv("NOMOS_TEST_LIVE_CONFIG"):
    pytest.skip("opt-in existing Nomos council/executor test", allow_module_level=True)

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from nemo_relay_nomos.nomos_client import HttpExecutor, McpCouncil
from nemo_relay_nomos.settings import Settings
from nemo_relay_nomos.verify import decode_writ, verify_executor_evidence, verify_writ
from nemo_relay_nomos.wire import canonical


@pytest.fixture(scope="module")
def live():
    cfg = json.loads(Path(os.environ["NOMOS_TEST_LIVE_CONFIG"]).read_text())
    s = Settings.from_config(cfg, environ=os.environ)
    return s, McpCouncil(s.connection), HttpExecutor(s.connection, s.limits.max_result_bytes)


def fields(s: Settings, command: str, *, justification: str, certified: list | None = None) -> dict:
    return {
        "agent_id": s.connection.agent_id,
        "auth_token": s.connection.auth_token,
        "council_id": s.connection.council_id,
        "command": command,
        "working_directory": s.connection.working_directory,
        "description": f"Print a line with {command.split(' ', 1)[-1]!r} to check the workspace shell.",
        "justification": justification,
        "target_resource": "stdout of the workspace shell",
        "blast_radius": "None: prints one line, touches no files or services.",
        "context": "tool=run_shell; latest user request: check the workspace shell",
        "evidence": [],
        "certified_evidence": certified or [],
    }


async def approved_run(s, council, executor) -> tuple[dict, dict, str]:
    """An approved echo, run once: (the writ decoded, the Executor's result, the writ as sent)."""
    mark = f"nomos-contract-{uuid.uuid4().hex[:8]}"
    decision = await council.authorize(
        **fields(s, f"/bin/echo {mark}", justification="The user asked to check the shell; a read-only echo.")
    )
    assert decision.get("status") == "approved", json.dumps(decision)[:2000]
    writ = decode_writ(decision["writ"])
    result = await executor.execute(decision["writ"], s.connection.agent_id)
    assert result["success"] and result["stdout"].strip() == mark, json.dumps(result)[:2000]
    return writ, result, decision["writ"]


async def test_live_the_council_serves_the_key_the_operator_pinned(live):
    """What trust.council_public_key_source = "council" would fetch is the key that verifies the Council's writs."""
    s, council, _ = live
    reply = await council.public_key(s.connection.council_id)
    assert not reply.get("error") and reply.get("council_id") == s.connection.council_id, reply
    assert reply["public_key_hex"] == s.trust.council_public_key_hex, reply


async def test_live_writs_and_results_verify_against_the_operators_trust_snapshot(live):
    s, council, executor = live
    trust = s.trust.as_dict()
    writ, result, _ = await approved_run(s, council, executor)
    verify_writ(
        writ,
        trust,
        agent_id=s.connection.agent_id,
        council_id=s.connection.council_id,
        command=writ["command"],
        working_directory=s.connection.working_directory,
        writ_id=writ["writ_id"],
    )
    verify_executor_evidence(
        result, writ, trust, agent_id=s.connection.agent_id, council_id=s.connection.council_id
    )


async def test_live_the_executor_runs_a_writ_at_most_once(live):
    s, council, executor = live
    _, _, encoded = await approved_run(s, council, executor)
    try:
        again = await executor.execute(encoded, s.connection.agent_id)
    except Exception as exc:  # noqa: BLE001 - an HTTP refusal is also a refusal
        again = {"success": False, "error": f"{type(exc).__name__}: {exc}"}
    print(
        "second use of one writ:",
        json.dumps({k: again.get(k) for k in ("success", "error", "verification_stage")}),
    )
    assert again.get("success") is False, json.dumps(again)[:2000]


async def test_live_the_council_validates_certified_evidence_it_is_sent(live):
    """Untouched: decided. Altered output, or a key the Council has not registered: an error, never a decision."""
    s, council, executor = live
    _, result, _ = await approved_run(s, council, executor)
    cite = "The cited evidence shows the earlier echo succeeded; this records it."

    untouched = await council.authorize(
        **fields(s, "/bin/echo noted", justification=cite, certified=[result])
    )
    assert untouched.get("status") in ("approved", "denied"), json.dumps(untouched)[:2000]

    tampered = copy.deepcopy(result)
    tampered["stdout"] = "forged output\n"
    refused = await council.authorize(
        **fields(s, "/bin/echo noted", justification=cite, certified=[tampered])
    )
    print("tampered:", json.dumps({k: refused.get(k) for k in ("status", "error")}))
    assert refused.get("status") == "error" and "stdout hash" in str(refused.get("error")), json.dumps(
        refused
    )[:2000]

    stranger = Ed25519PrivateKey.generate()
    public = stranger.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    resigned = copy.deepcopy(result)
    evidence = resigned["evidence"]
    evidence["public_key_hex"] = public.hex()
    evidence["key_fingerprint"] = evidence["payload"]["executor_key_fingerprint"] = hashlib.sha256(
        public
    ).hexdigest()
    evidence["signature"] = stranger.sign(canonical(evidence["payload"])).hex()
    refused = await council.authorize(
        **fields(s, "/bin/echo noted", justification=cite, certified=[resigned])
    )
    print("unregistered key:", json.dumps({k: refused.get(k) for k in ("status", "error")}))
    assert refused.get("status") == "error" and "not registered" in str(refused.get("error")), json.dumps(
        refused
    )[:2000]
