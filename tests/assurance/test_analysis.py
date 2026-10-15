# SPDX-License-Identifier: Apache-2.0
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("trace_ingest", reason="optional pinned Compass checkout required")

from assurance.analysis import analyze, project_traces
from assurance.fixtures import scenarios


@pytest.mark.asyncio
async def test_live_cases_produce_expected_reviews_without_cross_run_contamination():
    from assurance.fixtures import live_scenario

    expected = {
        "denial": {"repeated_unchanged_denial": 1},
        "retry-loop": {"explicit_tool_failure": 9, "repeated_identical_failed_call": 3},
        "verification": {"verification_failure": 1},
        "benign": {},
    }
    entries, cases = [], []
    for run_id in ("first", "second"):
        for name, counts in expected.items():
            events = live_scenario(name, run_id, "demo")
            start = len(entries) + 1
            entries.extend(
                event(start + i, kind, payload) for i, (kind, payload) in enumerate(events)
            )
            cases.append((events[0][1]["agent_id"], counts, set(range(start, len(entries) + 1))))
    findings, coverage = await analyze(entries, "c")
    assert coverage["compass_problems"] >= 1
    for agent, counts, sequences in cases:
        actual = [f for f in findings if f["agent_id"] == agent]
        assert dict(Counter(f["kind"] for f in actual)) == counts
        assert all(f["synthetic"] is True for f in actual)
        assert all(ref["sequence"] in sequences for f in actual for ref in f["evidence"])


def event(seq, kind, payload, minutes=0):
    return {
        "sequence": seq,
        "entry_type": kind,
        "payload": payload,
        "timestamp": (datetime(2026, 10, 8, tzinfo=UTC) + timedelta(minutes=minutes)).isoformat(),
        "content_hash": f"sha256:{seq:064x}",
    }


def review(agent, command, decision="deny"):
    return {
        "agent_id": agent,
        "intent": {"command": command, "working_directory": "/demo"},
        "final": {"decision": decision},
        "schema_version": 1,
    }


@pytest.mark.asyncio
async def test_denial_rule_requires_same_agent_command_and_window():
    entries = [event(i, "intent_review_recorded", review("a", "blocked")) for i in range(3)]
    findings, _ = await analyze(entries, "c")
    assert [f["kind"] for f in findings] == ["repeated_unchanged_denial"]
    assert findings[0]["agent_id"] == "a"
    assert len(findings[0]["evidence"]) == 3
    entries[2] = event(2, "intent_review_recorded", review("b", "blocked"))
    assert not (await analyze(entries, "c"))[0]
    entries[2] = event(2, "intent_review_recorded", review("a", "corrected", "approve"))
    assert not (await analyze(entries, "c"))[0]
    entries[2] = event(2, "intent_review_recorded", review("a", "blocked"), minutes=16)
    assert not (await analyze(entries, "c"))[0]


def test_review_is_guardrail_and_unattributed_execution_not_invented():
    traces, coverage = project_traces(
        [
            event(1, "intent_review_recorded", review("a", "secret command")),
            event(2, "execution_failed", {"token_id": "x", "error": "failed"}),
        ],
        "c",
    )
    assert traces[0].root_spans[0].kind.value == "GUARDRAIL"
    assert coverage["unsupported_execution_events"] == 1
    assert coverage["tool_spans"] == 0


@pytest.mark.asyncio
async def test_real_compass_detects_fixture_retries_and_preserves_evidence():
    entries = [event(i, kind, payload) for i, (kind, payload) in enumerate(scenarios(), 1)]
    findings, coverage = await analyze(entries, "c")
    kinds = {f["kind"] for f in findings}
    assert {
        "repeated_unchanged_denial",
        "repeated_identical_failed_call",
        "explicit_tool_failure",
        "verification_failure",
    } <= kinds
    assert coverage["compass_problems"] >= 1
    assert coverage["tool_spans"] == 9
    assert all(f["agent_id"] != "careful-agent" for f in findings)
    compass = [f for f in findings if f["detector"].startswith("compass")]
    assert all(f["evidence"] and f["evidence"][0]["chain_id"] == "c" for f in compass)
    assert "secret command" not in str(findings)
    again, _ = await analyze(entries, "c")
    assert {f["id"] for f in again} == {f["id"] for f in findings}


@pytest.mark.asyncio
async def test_retry_finding_links_every_supporting_call():
    entries = [event(i, kind, payload) for i, (kind, payload) in enumerate(scenarios(), 1)]
    findings, coverage = await analyze(entries, "c")
    retries = [f for f in findings if f["kind"] == "repeated_identical_failed_call"]
    assert retries and all(len(f["evidence"]) == 3 for f in retries)
    assert coverage["limitations"]


def receipt(writ_id="writ-1", *, success=False):
    return {
        "executor_id": "executor-one",
        "executor_key_fingerprint": "a" * 64,
        "writ_id": writ_id,
        "command": "/bin/false" if not success else "/bin/true",
        "success": success,
        "exit_code": 0 if success else 1,
        "stdout_sha256": "b" * 64,
        "stderr_sha256": "c" * 64,
    }


def certified_review(seq, receipts, agent="agent-one"):
    payload = review(agent, "/bin/true", "approve")
    payload.update(council_id="live", intent_id=f"intent-{seq}")
    payload["intent"].update(
        agent_id=agent,
        council_id="live",
        intent_id=f"intent-{seq}",
        metadata={"certified_evidence": receipts},
    )
    return event(seq, "intent_review_recorded", payload)


@pytest.mark.asyncio
async def test_council_certified_receipts_keep_guardrail_and_real_outcome_provenance():
    entries = [certified_review(1, [receipt("failed"), receipt("okay", success=True)])]
    traces, coverage = project_traces(entries, "ledger-live")
    spans = [span for trace in traces for span in trace.root_spans]
    assert [span.kind.value for span in spans].count("GUARDRAIL") == 1
    tools = [span for span in spans if span.kind.value == "TOOL"]
    assert len(tools) == coverage["certified_evidence_tool_spans"] == 2
    assert tools[0].input == {"command": "/bin/false"}
    assert tools[0].output == {
        "success": False,
        "exit_code": 1,
        "stdout_sha256": "b" * 64,
        "stderr_sha256": "c" * 64,
    }
    assert tools[0].attributes["source_pointer"]["payload_path"] == (
        "/intent/metadata/certified_evidence/0"
    )
    assert tools[0].attributes["executor_signature_available"] is False
    assert "council" in coverage["certified_evidence_attribution"]
    findings, _ = await analyze(entries, "ledger-live")
    assert [f["kind"] for f in findings] == ["explicit_tool_failure"]
    assert findings[0]["agent_id"] == "agent-one"
    assert findings[0]["synthetic"] is False
    assert findings[0]["evidence"] == [tools[0].attributes["source_pointer"]]


@pytest.mark.asyncio
async def test_duplicate_receipts_do_not_invent_retries_or_independent_cases():
    entries = [certified_review(i, [receipt()]) for i in range(1, 5)]
    findings, coverage = await analyze(entries, "ledger-live")
    assert [f["kind"] for f in findings] == ["explicit_tool_failure"]
    assert coverage["review_spans"] == 4
    assert coverage["certified_evidence_summaries"] == 4
    assert coverage["certified_evidence_tool_spans"] == 1
    assert coverage["certified_evidence_duplicates"] == 3
    assert coverage["compass_problems"] == 0


@pytest.mark.asyncio
async def test_receipt_finding_id_survives_original_review_leaving_analysis_window():
    earlier, _ = await analyze([certified_review(1, [receipt()])], "ledger-live")
    later, _ = await analyze([certified_review(2001, [receipt()])], "ledger-live")
    assert earlier[0]["id"] == later[0]["id"]
    assert earlier[0]["evidence"][0]["sequence"] == 1
    assert later[0]["evidence"][0]["sequence"] == 2001


@pytest.mark.asyncio
async def test_unscoped_receipts_never_promote_independent_cases_even_across_agents():
    entries = [
        certified_review(i, [receipt(f"writ-{i}")], agent=f"agent-{i % 3}") for i in range(1, 7)
    ]
    findings, coverage = await analyze(entries, "ledger-live")
    assert Counter(f["kind"] for f in findings) == {"explicit_tool_failure": 6}
    assert coverage["compass_problems"] == 0
    assert all(card["cases"] == 0 and not card["eligible"] for card in coverage["recurring_cards"])
    assert sum(card["unscoped_observations"] for card in coverage["recurring_cards"]) == 6
    traces, _ = project_traces(entries, "ledger-live")
    by_agent = {}
    for trace in traces:
        if trace.root_spans[0].kind.value == "TOOL":
            assert len(trace.root_spans) == 1
            by_agent.setdefault(trace.attributes["agent_id"], set()).add(
                trace.attributes["logical_case_id"]
            )
    assert all(len(cases) == 1 for cases in by_agent.values())


@pytest.mark.asyncio
async def test_distinct_receipts_in_one_review_have_distinct_finding_ids():
    findings, _ = await analyze(
        [certified_review(1, [receipt("first"), receipt("second")])], "ledger-live"
    )
    assert len(findings) == len({f["id"] for f in findings}) == 2


@pytest.mark.parametrize("change", ["outcome", "agent", "executor", "malformed"])
@pytest.mark.asyncio
async def test_conflicting_writ_receipts_are_suppressed(change):
    first = certified_review(1, [receipt()])
    second = certified_review(2, [receipt()])
    summary = second["payload"]["intent"]["metadata"]["certified_evidence"][0]
    if change == "outcome":
        summary.update(success=True, exit_code=0)
    elif change == "agent":
        second["payload"]["agent_id"] = second["payload"]["intent"]["agent_id"] = "other"
    elif change == "executor":
        summary["executor_id"] = "other"
    else:
        summary["success"] = "false"
    findings, coverage = await analyze([first, second], "ledger-live")
    assert findings == []
    assert coverage["tool_spans"] == 0
    assert coverage["certified_evidence_conflicting_writs"] == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("success", "false"),
        ("exit_code", True),
        ("executor_key_fingerprint", "not-a-fingerprint"),
        ("stdout_sha256", "invalid"),
        ("writ_id", ""),
        ("command", ""),
    ],
)
def test_malformed_certified_evidence_is_not_coerced(field, value):
    summary = receipt()
    summary[field] = value
    _, coverage = project_traces([certified_review(1, [summary])], "ledger-live")
    assert coverage["tool_spans"] == 0
    assert coverage["certified_evidence_rejected"] == 1


@pytest.mark.parametrize("field", ["agent_id", "council_id", "intent_id"])
def test_certified_evidence_requires_matching_signed_envelope(field):
    entry = certified_review(1, [receipt()])
    entry["payload"]["intent"][field] = "different"
    _, coverage = project_traces([entry], "ledger-live")
    assert coverage["tool_spans"] == 0
    assert coverage["certified_evidence_rejected"] == 1


def test_raw_agent_evidence_outer_metadata_and_other_councils_are_not_certified():
    entry = certified_review(1, [receipt()])
    _, coverage = project_traces([entry], "ledger-other")
    assert coverage["tool_spans"] == 0
    altered = deepcopy(entry)
    altered["payload"]["intent"].pop("metadata")
    altered["payload"]["intent"]["evidence"] = [receipt()]
    altered["metadata"] = {"certified_evidence": [receipt()]}
    _, coverage = project_traces([altered], "ledger-live")
    assert coverage["tool_spans"] == 0
    assert coverage["certified_evidence_summaries"] == 0
