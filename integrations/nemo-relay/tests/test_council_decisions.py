# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""With a signing_key, a Council answer that authorized nothing is certified by the plugin, and every citation is
certified evidence: nothing is sent as asserted ``evidence``."""

import json
from copy import deepcopy

import pytest
from support import Council, Next, base_config, call, cite, turn
from test_tool_definitions import nomos_contract

from nemo_relay_nomos.plugin import COUNCIL_DECISION_COMMAND
from nemo_relay_nomos.records import citation, integrity_ok
from nemo_relay_nomos.verify import EvidenceError, verify_plugin_certified
from nemo_relay_nomos.wire import sha256_hex


def certified_result(p, data):
    return p.store.get(data["evidence_id"])["certified_result"]


@pytest.mark.parametrize(
    ("status", "kind", "result_status", "guidance"),
    [
        ("denied", "council_denial", "denied", ["Use absolute executable paths."]),
        ("raise", "council_error", "council_error", []),
    ],
)
async def test_a_council_answer_that_ran_nothing_is_certified(setup, status, kind, result_status, guidance):
    # "raise": the Council is unreachable, so the plugin writes the error itself; that answer is certified too.
    p, *_ = setup(status)
    data = (await p.h3(call(turn=turn(p), call_id="c-7"), Next())).result
    assert data["status"] == result_status and data["evidence_status"] == "plugin_certified"
    record = p.store.get(data["evidence_id"])
    assert record["kind"] == kind and record["certified"] and record["evidence_status"] == "plugin_certified"
    assert integrity_ok(record, p.signer.public_key_hex)

    result = record["certified_result"]
    payload = result["evidence"]["payload"]
    assert result["executor_id"] == payload["executor_id"] == p.signer.executor_id
    assert (result["writ_id"], payload["intent_hash"], payload["nonce"]) == ("", "", ""), (
        "no writ: nothing ran"
    )
    assert payload["command"]["command"] == COUNCIL_DECISION_COMMAND.format(tool="run_shell")
    assert result["success"] is False and "authorized nothing" in result["error"], "nothing was authorized"
    assert (payload["council_id"], payload["target_agent_id"], payload["request_id"]) == (
        "council",
        "agent",
        "c-7",
    )
    observed = json.loads(result["stdout"])
    assert observed == {
        "type": "nomos.council_decision",
        "version": 1,
        "tool": "run_shell",
        "tool_call_id": "c-7",
        "decision_id": record["decision_id"],
        "arguments_sha256": record["arguments_sha256"],
        "status": result_status,
        "guidance": guidance,
        "missing_permissions": [],
        "retryable": False,
    }
    assert result["stdout"].isascii()


async def test_nomos_parses_and_verifies_a_certified_decision_with_its_own_contract(setup):
    contract = nomos_contract()
    p, *_ = setup("denied")
    sent = certified_result(p, (await p.h3(call(turn=turn(p)), Next())).result)
    evidence = contract.ExecutionResult.model_validate(sent).evidence
    assert evidence.verify(), "Nomos's ExecutionEvidence.verify() over its own canonical bytes"
    assert evidence.payload.executor_key_fingerprint == evidence.key_fingerprint
    assert sha256_hex(sent["stdout"].encode()) == evidence.payload.result.stdout_sha256


async def test_a_cited_denial_goes_to_the_council_as_certified_evidence(setup):
    p, council, *_ = setup("denied")
    t = turn(p)
    denied = (await p.h3(call(turn=t), Next())).result
    council.status = "approved"
    later = await p.h3(cite(t, denied["evidence_id"], call_id="c2"), Next())
    sent = council.calls[1]
    assert sent["certified_evidence"] == [certified_result(p, denied)] and sent["evidence"] == []
    assert later.annotation["nomos"]["submitted"]["certified_citations"] == [denied["evidence_id"]]
    assert "asserted_citations" not in later.annotation["nomos"]["submitted"]


async def test_a_cited_decision_that_fails_the_recheck_is_refused_before_the_council(setup, monkeypatch):
    p, council, *_ = setup("denied")
    t = turn(p)
    denied = (await p.h3(call(turn=t), Next())).result
    altered = deepcopy(certified_result(p, denied))
    altered["stdout"] = altered["stdout"].replace("denied", "approved")
    monkeypatch.setattr("nemo_relay_nomos.plugin.citation", lambda record: altered)
    out = await p.h3(cite(t, denied["evidence_id"], call_id="c2"), Next())
    assert out.result["status"] == "output_digest_mismatch"
    assert len(council.calls) == 1, "the Council is not asked"


async def test_the_recheck_binds_a_decision_to_no_writ(setup):
    p, *_ = setup("denied")
    result = certified_result(p, (await p.h3(call(turn=turn(p)), Next())).result)
    check = dict(executor_id=p.signer.executor_id, agent_id="agent", council_id="council")
    verify_plugin_certified(result, p.signer.public_key_hex, writ_id="", **check)
    with pytest.raises(EvidenceError, match="result_identity_mismatch"):
        verify_plugin_certified(result, p.signer.public_key_hex, writ_id="a-writ", **check)


async def test_a_refusal_is_never_cited(setup):
    p, council, *_ = setup()
    t = turn(p)
    refused = (await p.h3(cite(t, "ev_" + "0" * 32), Next())).result  # a refusal, stored in this conversation
    assert refused["status"] == "evidence_not_found" and refused["evidence_status"] == "plugin_signed"
    record = p.store.get(refused["evidence_id"])
    assert record["kind"] == "plugin_refusal" and not record["certified"] and citation(record) is None
    out = await p.h3(cite(t, refused["evidence_id"], call_id="c2"), Next())
    assert out.result["status"] == "evidence_not_citable"
    assert council.calls == [], "the Council is never asked"


async def test_nothing_is_ever_sent_as_asserted_evidence(setup):
    p, council, *_ = setup("denied")
    t = turn(p)
    ids = [(await p.h3(call(turn=t, call_id=f"c{n}"), Next())).result["evidence_id"] for n in range(2)]
    council.status = "approved"
    ran = (await p.h3(call(turn=t, call_id="c3"), Next())).result
    await p.h3(cite(t, *ids, ran["evidence_id"], call_id="c4"), Next())
    assert all(sent["evidence"] == [] for sent in council.calls)
    assert len(council.calls[-1]["certified_evidence"]) == 3


async def test_without_a_signing_key_a_denial_is_neither_stored_nor_certified(setup):
    cfg = base_config()
    del cfg["signing_key"]
    p, *_ = setup("denied", config=cfg)
    data = (await p.h3(call(turn=turn(p)), Next())).result
    assert (
        data["status"] == "denied" and "evidence_id" not in data and data["evidence_status"] == "unavailable"
    )
    assert p.store.db.execute("SELECT count(*) FROM records").fetchone()[0] == 0


async def test_a_tampered_certified_decision_fails_the_record_receipt(setup):
    p, *_ = setup("denied")
    record = p.store.get((await p.h3(call(turn=turn(p)), Next())).result["evidence_id"])
    tampered = deepcopy(record)
    tampered["certified_result"]["stdout"] = tampered["certified_result"]["stdout"].replace(
        "denied", "approved"
    )
    assert not integrity_ok(tampered, p.signer.public_key_hex)


class VerboseCouncil(Council):
    """A denial that quotes the agent at length, with more guidance and permissions than the model is shown."""

    async def authorize(self, **fields):
        self.calls.append(fields)
        quote = "AGENT-CLAIM: the previous command printed 'ERROR: disk full'"
        return {
            "status": "denied",
            "decision_id": "d" * 400,
            "error": f"Denied. {quote}",
            "guidance": ["g" * 5000] * 9,
            "missing_permissions": ["p" * 1000] * 30,
            "retryable": True,
            "council_members": [
                {"member_id": f"m{n}", "decision": "deny", "reasoning": quote} for n in range(20)
            ],
            "chairman": {"reasoning": quote},
            "debate_history": [quote] * 10,
        }


async def test_a_certified_decision_carries_only_its_structured_parts(setup):
    # The model still sees the members' reasoning; the certificate never carries it back to the Council.
    p, *_ = setup(council=VerboseCouncil())
    data = (await p.h3(call(turn=turn(p)), Next())).result
    assert "AGENT-CLAIM" in json.dumps(data["council"])
    result = certified_result(p, data)
    assert "AGENT-CLAIM" not in result["stdout"]
    observed = json.loads(result["stdout"])
    assert set(observed) == {
        "type",
        "version",
        "tool",
        "tool_call_id",
        "decision_id",
        "arguments_sha256",
        "status",
        "guidance",
        "missing_permissions",
        "retryable",
    }
    assert len(observed["guidance"]) == 5 and all(len(g) <= 1024 + 40 for g in observed["guidance"])
    assert len(observed["missing_permissions"]) == 20
    assert all(len(m) <= 256 + 40 for m in observed["missing_permissions"])
    assert len(observed["decision_id"]) <= 256 + 40 and observed["retryable"] is True
    assert len(result["stdout"]) < 16384, "bounded by construction"
