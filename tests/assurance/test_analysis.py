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


def test_signed_review_projects_existing_context_without_turning_approval_into_execution():
    entry = certified_review(1, [])
    entry["payload"]["intent"].update(
        description="Deploy service",
        justification="Release fix",
        target_environment="staging",
        parameters={"replicas": 2},
        metadata={"raw_secret": "not projected"},  # pragma: allowlist secret
    )
    entry["payload"]["chairman"] = {"risk_level": "low"}
    traces, _ = project_traces([entry], "ledger-live")
    span = traces[0].root_spans[0]
    assert span.input["description"] == "Deploy service"
    assert span.input["target_environment"] == "staging"
    assert span.output["chairman"] == {"risk_level": "low"}
    assert traces[0].attributes["evidence_scopes"] == ["request"]
    assert "raw_secret" not in str(span.model_dump())


@pytest.mark.asyncio
async def test_native_execution_requires_explicit_chain_token_and_intent_links():
    entries = [
        certified_review(1, []),
        event(
            2,
            "execution_started",
            {"intent_id": "intent-1", "token_id": "token", "action": {"command": "true"}},
        ),
        event(
            3,
            "execution_completed",
            {"token_id": "token", "action_type": "execute", "output": {"success": True}},
        ),
    ]
    traces, coverage = project_traces(entries, "ledger-live")
    assert coverage["correlated_execution_spans"] == 1
    tool = next(t for t in traces if t.root_spans[0].kind.value == "TOOL")
    assert tool.attributes["agent_id"] == "agent-one"
    assert tool.attributes["correlation_scope"] == "unavailable"
    assert tool.root_spans[0].tool_call.result_id is None
    assert len(tool.root_spans[0].attributes["related_sources"]) == 2
    _, no_link = project_traces([entries[0], entries[2]], "ledger-live")
    assert no_link["tool_spans"] == 0
    _, wrong_chain = project_traces(entries, "ledger-other")
    assert wrong_chain["tool_spans"] == 0


@pytest.mark.asyncio
async def test_preserve_nested_order_missing_result_and_genuine_result_references():
    entry = event(
        1,
        "execution_failed",
        {
            "assurance_schema": 1,
            "agent_id": "a",
            "run_id": "r",
            "logical_case_id": "case",
            "tool_name": "parent",
            "call_id": "parent",
            "input": {},
            "assurance_context": {
                "schema_version": 1,
                "children": [
                    {
                        "id": "child",
                        "kind": "TOOL",
                        "tool_name": "child",
                        "input": {},
                        "output": {"success": False},
                        "tool_call": {"call_id": "child", "result_id": "other", "index": 1},
                    }
                ],
                "tool_catalog": {"parent": None, "child": None},
            },
        },
    )
    traces, _ = project_traces([entry], "c")
    parent = traces[0].root_spans[0]
    from trace_ingest.models import UNSET

    assert parent.output is UNSET
    assert parent.tool_call.result_id is None
    assert parent.children[0].tool_call.result_id == "other"
    findings, coverage = await analyze([entry], "c")
    assert {f["kind"] for f in findings} >= {
        "missing_tool_result",
        "call_result_id_mismatch",
        "explicit_tool_failure",
    }
    assert all(f["evidence"][0]["sequence"] == 1 for f in findings)
    assert coverage["tool_rule_coverage"]["rules"]["total"] == 19
    assert coverage["tool_rule_coverage"]["field_presence"]["result_id"] == 1


@pytest.mark.asyncio
async def test_tool_coverage_reports_missing_catalog_schemas_without_claiming_full_coverage():
    entries = [event(i, kind, payload) for i, (kind, payload) in enumerate(scenarios(), 1)]
    _, coverage = await analyze(entries, "c")
    rules = coverage["tool_rule_coverage"]["rules"]
    assert "missing_required_argument" in rules["abstaining"]
    assert rules["evaluable"] < rules["total"]


@pytest.mark.asyncio
async def test_structured_tool_authorization_is_request_evidence_without_execution():
    entry = certified_review(1, [])
    entry["payload"]["intent"].update(
        command="",
        context="A user requested a staging release",
        operation={
            "kind": "tool_call",
            "schema_version": 1,
            "tool_name": "deploy",
            "arguments_sha256": "a" * 64,
            "canonicalization": "nomos-json-v1",
            "subject": "tenant-one",
        },
    )
    traces, coverage = project_traces([entry], "ledger-live")
    assert coverage["structured_tool_requests"] == 1
    assert coverage["tool_spans"] == 0
    span = traces[0].root_spans[0]
    assert span.kind.value == "GUARDRAIL"
    assert span.input["operation"]["tool_name"] == "deploy"
    assert span.input["context"] == "A user requested a staging release"
    assert "arguments" not in span.input["operation"]
    assert not (await analyze([entry], "ledger-live"))[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("grounded", [True, False])
async def test_complete_conversation_grounds_rejected_identifier(grounded):
    entry = event(
        1,
        "execution_failed",
        {
            "assurance_schema": 1,
            "agent_id": "a",
            "run_id": "r",
            "logical_case_id": "case",
            "tool_name": "lookup",
            "call_id": "call",
            "input": {"customer_id": "customer-123"},
            "output": {"error": "customer-123 not found"},
            "assurance_context": {
                "schema_version": 1,
                "conversation_complete": True,
                "messages": [
                    {
                        "role": "user",
                        "content": "Look up customer-123" if grounded else "Look up the customer",
                    }
                ],
            },
        },
    )
    findings, _ = await analyze([entry], "c")
    kinds = {f["kind"] for f in findings}
    assert "explicit_tool_failure" in kinds
    assert ("explicitly_rejected_ungrounded_identifier" in kinds) is not grounded
    trace = project_traces([entry], "c")[0][0]
    assert (
        trace.root_spans[0].tool_call.prior_user_text
        == entry["payload"]["assurance_context"]["messages"][0]["content"]
    )


@pytest.mark.asyncio
async def test_signed_nested_null_and_orphan_results_publish_for_both_streams(
    tmp_path, monkeypatch
):
    import io
    import json
    import zipfile

    from assurance.export import build_bundle
    from assurance.monitor import Monitor, Store
    from assurance.scheduler import StreamWorker
    from nomos.level_0.crypto import KeyPair, Signer
    from nomos.level_1.ledger.chain import LedgerChain
    from nomos.level_1.ledger.entry import LedgerEntryType

    key = KeyPair.generate("orphan-test")
    chain = LedgerChain(signer=Signer(key), chain_id="c")
    chain.initialize("signed synthetic test")
    chain.append(
        LedgerEntryType.EXECUTION_FAILED,
        {
            "assurance_schema": 1,
            "agent_id": "a",
            "run_id": "r",
            "logical_case_id": "case",
            "tool_name": "lookup",
            "call_id": "call",
            "input": {},
            "output": {"success": False},
            "assurance_context": {
                "schema_version": 1,
                "conversation_complete": True,
                "messages": [{"role": "user", "content": "Use staging only."}],
                "children": [
                    {
                        "id": "nested-tool",
                        "kind": "TOOL",
                        "tool_name": "lookup",
                        "input": {},
                        "output": {"success": True},
                        "tool_call": None,
                    }
                ],
                "orphan_results": [
                    {
                        "result_id": "unmatched-call",
                        "chain_id": "forged",
                        "sequence": 900,
                        "content_hash": "forged",
                    }
                ],
            },
        },
    )

    class Source:
        async def page(self, cursor=None, limit=100):
            return {
                "chain_id": "c",
                "head_sequence": 1,
                "entries": [chain.get(i).to_dict() for i in (1, 0)],
            }

        async def get(self, sequence):
            return chain.get(sequence).to_dict()

    store = Store(tmp_path / "db", "c")
    await Monitor(Source(), store, {key.key_id: key.public_key_hex()}).ingest()
    worker = StreamWorker(store, "tool-issues", analyze, config={})
    await worker.step()
    assert worker.progress() == 1
    from insight_agent.compass_signals.compass_signals import CompassSignalResult

    from assurance.ethos import EthosRegistry, InferenceConfig, analyze_ethos

    (tmp_path / "ETHOS.md").write_text("# EXEC-01\nUse staging only.")
    (tmp_path / "ethos.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "documents": [
                    {
                        "id": "example",
                        "version": "1",
                        "path": "ETHOS.md",
                        "effective_from": "2020-01-01T00:00:00Z",
                        "requirements": [
                            {
                                "id": "EXEC-01",
                                "text": "Use staging only.",
                                "evidence": ["execution"],
                            }
                        ],
                    }
                ],
                "agents": {"a": ["example@1"]},
            }
        )
    )
    registry = EthosRegistry.from_file(tmp_path / "ethos.json")
    inference = InferenceConfig(
        model="openai/fixture",
        api_base="https://approved.example/v1",
        api_key="fixture-only",  # pragma: allowlist secret
    )

    async def detect(snapshot, text, inference, budget):
        nested = next(iter(snapshot)).root_spans[0].children[0]
        assert nested.tool_call.prior_user_text == "Use staging only."
        return CompassSignalResult(signal_name="ethos-divergence", problems=())

    monkeypatch.setattr("assurance.ethos._detect", detect)

    async def ethos(events, chain_id):
        return await analyze_ethos(events, chain_id, registry, inference)

    ethos_worker = StreamWorker(store, "ethos-divergence", ethos, config={})
    outcome = await ethos_worker.step()
    assert outcome["status"] == "complete"
    assert ethos_worker.progress() == 1
    item = next(f for f in store.findings() if f["kind"] == "orphan_tool_result")
    assert item["evidence"] == [
        {
            "result_id": "unmatched-call",
            "chain_id": "c",
            "sequence": 1,
            "content_hash": chain.get(1).to_dict()["content_hash"],
            "payload_path": "/assurance_context/orphan_results/0",
        }
    ]
    validator = tmp_path / "skills/eval-author-audit/scripts/audit_spec/validate.py"
    validator.parent.mkdir(parents=True)
    validator.write_text("import json; print(json.dumps({'valid': True}))")
    monkeypatch.setenv("EVAL_AUTHOR_ROOT", str(tmp_path))
    with zipfile.ZipFile(io.BytesIO(build_bundle(store))) as archive:
        exported = next(
            f
            for f in json.loads(archive.read("findings.json"))
            if f["kind"] == "orphan_tool_result"
        )
        assert exported["evidence"] == item["evidence"]


@pytest.mark.asyncio
async def test_one_complete_conversation_does_not_enable_run_wide_grounding_checks():
    payload = {
        "assurance_schema": 1,
        "agent_id": "a",
        "run_id": "r",
        "logical_case_id": "case",
        "tool_name": "lookup",
        "call_id": "first",
        "input": {"customer_id": "customer-123"},
        "output": {"error": "customer-123 not found"},
        "assurance_context": {
            "schema_version": 1,
            "conversation_complete": True,
            "messages": [{"role": "user", "content": "Find the customer"}],
        },
    }
    partial = {**payload, "call_id": "second", "assurance_context": {"schema_version": 1}}
    events = [event(1, "execution_failed", payload), event(2, "execution_failed", partial)]
    traces, _ = project_traces(events, "c")
    assert traces[0].attributes["complete_provenance_context"] is False
    findings, _ = await analyze(events, "c")
    assert "explicitly_rejected_ungrounded_identifier" not in {f["kind"] for f in findings}
