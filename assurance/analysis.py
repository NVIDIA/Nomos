# SPDX-License-Identifier: Apache-2.0
"""Deterministic advisory findings. Never authorize, punish, or classify intent."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, ValidationError

COMPASS_COMMIT = "11fefa0a6a2c20fa1eeda56447869b3a4fba88f8"  # pragma: allowlist secret
AUTHOR_COMMIT = "20f95a0a010d4905c13065678af51970404910c9"  # pragma: allowlist secret


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def reference(event: dict, chain_id: str) -> dict:
    return {
        "chain_id": chain_id,
        "sequence": event["sequence"],
        "content_hash": event["content_hash"],
    }


class ToolObservation(BaseModel):
    """Optional signed payload extension; never infer these IDs from metadata."""

    agent_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    logical_case_id: str = Field(min_length=1, max_length=200)
    tool_name: str = Field(min_length=1, max_length=200)
    call_id: str = Field(min_length=1, max_length=200)


class CertifiedExecutionSummary(BaseModel):
    """Council-validated receipt summary inside an authenticated ledger payload."""

    model_config = ConfigDict(strict=True)

    executor_id: str = Field(min_length=1, max_length=200)
    executor_key_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    writ_id: str = Field(min_length=1, max_length=200)
    command: str = Field(min_length=1, max_length=65536)
    success: bool
    exit_code: int | None
    stdout_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stderr_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _certified_traces(events: list[dict], chain_id: str) -> tuple[list[dict], dict]:
    """Project summaries, without claiming independent executor verification or runs."""
    coverage = {
        "certified_evidence_summaries": 0,
        "certified_evidence_tool_spans": 0,
        "certified_evidence_duplicates": 0,
        "certified_evidence_rejected": 0,
        "certified_evidence_conflicting_writs": 0,
        "certified_evidence_attribution": (
            "council-validated executor receipt summary in signed review payload; "
            "original executor signature and run/case correlation unavailable"
        ),
    }
    accepted, rejected_writs, conflicts = {}, set(), set()
    for event in sorted(events, key=lambda e: e["sequence"]):
        if event["entry_type"] != "intent_review_recorded":
            continue
        payload = event["payload"]
        intent = payload.get("intent")
        if not isinstance(intent, dict) or not isinstance(intent.get("metadata"), dict):
            continue
        summaries = intent["metadata"].get("certified_evidence", [])
        if not isinstance(summaries, list):
            coverage["certified_evidence_rejected"] += 1
            continue
        envelope_valid = (
            type(payload.get("schema_version")) is int
            and payload["schema_version"] == 1
            and all(
                isinstance(payload.get(field), str)
                and 0 < len(payload[field]) <= 200
                and payload[field].strip()
                and intent.get(field) == payload[field]
                for field in ("agent_id", "council_id", "intent_id")
            )
            and chain_id == f"ledger-{payload.get('council_id')}"
        )
        for index, summary in enumerate(summaries):
            coverage["certified_evidence_summaries"] += 1
            try:
                receipt = CertifiedExecutionSummary.model_validate(summary)
                if not envelope_valid:
                    raise ValueError("certified evidence envelope does not match chain/principal")
            except (ValueError, ValidationError):
                coverage["certified_evidence_rejected"] += 1
                writ_id = summary.get("writ_id") if isinstance(summary, dict) else None
                if isinstance(writ_id, str) and writ_id:
                    rejected_writs.add(writ_id)
                continue
            identity = digest(
                [payload["agent_id"], payload.get("synthetic") is True, receipt.model_dump()]
            )
            if receipt.writ_id in accepted:
                if accepted[receipt.writ_id][0] == identity:
                    coverage["certified_evidence_duplicates"] += 1
                else:
                    conflicts.add(receipt.writ_id)
            else:
                accepted[receipt.writ_id] = (identity, receipt, event, index)
    conflicts.update(rejected_writs.intersection(accepted))
    coverage["certified_evidence_conflicting_writs"] = len(conflicts)
    traces = []
    for writ_id, (_, receipt, event, index) in accepted.items():
        if writ_id in conflicts:
            continue
        payload = event["payload"]
        tid = f"{chain_id}:certified:{digest(writ_id)[:24]}"
        traces.append(
            {
                "id": tid,
                "root_spans": [
                    {
                        "id": tid,
                        "kind": "TOOL",
                        "tool_name": "nomos.certified_execution",
                        "tool_call": {"call_id": writ_id, "result_id": writ_id, "index": 0},
                        "input": {"command": receipt.command},
                        "output": receipt.model_dump(
                            include={"success", "exit_code", "stdout_sha256", "stderr_sha256"}
                        ),
                        "attributes": {
                            "source_pointer": {
                                **reference(event, chain_id),
                                "payload_path": f"/intent/metadata/certified_evidence/{index}",
                            },
                            "executor_id": receipt.executor_id,
                            "executor_key_fingerprint": receipt.executor_key_fingerprint,
                            "executor_signature_available": False,
                            "attribution": "council-validated executor receipt summary",
                        },
                    }
                ],
                "attributes": {
                    "agent_id": payload["agent_id"],
                    "synthetic": payload.get("synthetic") is True,
                    # A receipt is one execution. Its review is not its original run.
                    "logical_case_id": f"unscoped:{digest(payload['agent_id'])[:24]}",
                    "correlation_scope": "unavailable",
                },
            }
        )
    coverage["certified_evidence_tool_spans"] = len(traces)
    return traces, coverage


def project_traces(events: list[dict], chain_id: str):
    from trace_ingest.models import Trace

    grouped = {}
    coverage = {
        "events_in_window": len(events),
        "window_limit": 2000,
        "tool_spans": 0,
        "review_spans": 0,
        "unsupported_execution_events": 0,
        "attribution": "signed ledger payload; writer-asserted agent identity",
    }
    for event in events:
        payload, kind = event["payload"], event["entry_type"]
        ref = reference(event, chain_id)
        attributes = {"source_pointer": ref}
        if kind == "intent_review_recorded":
            tid = f"{chain_id}:review:{event['sequence']}"
            grouped[tid] = {
                "id": tid,
                "root_spans": [
                    {
                        "id": tid,
                        "kind": "GUARDRAIL",
                        "output": payload.get("final", {}),
                        "attributes": attributes,
                    }
                ],
                "attributes": {
                    "agent_id": payload.get("agent_id", "unattributed"),
                    "synthetic": payload.get("synthetic") is True,
                },
            }
            coverage["review_spans"] += 1
        elif kind in {"execution_started", "execution_completed", "execution_failed"}:
            try:
                if payload.get("assurance_schema") != 1 or kind == "execution_started":
                    raise ValueError("no complete signed observation")
                observation = ToolObservation.model_validate(payload)
            except (ValidationError, ValueError):
                coverage["unsupported_execution_events"] += 1
                continue
            # Scope run IDs to both chain and principal; don't merge two agents' runs.
            tid = f"{chain_id}:run:{digest([observation.agent_id, observation.run_id])[:24]}"
            trace = grouped.setdefault(
                tid,
                {
                    "id": tid,
                    "root_spans": [],
                    "attributes": {
                        "agent_id": observation.agent_id,
                        "logical_case_id": observation.logical_case_id,
                        "synthetic": payload.get("synthetic") is True,
                    },
                },
            )
            span = {
                "id": f"{chain_id}:{event['sequence']}",
                "kind": "TOOL",
                "tool_name": observation.tool_name,
                "tool_call": {
                    "call_id": observation.call_id,
                    "result_id": observation.call_id,
                    "index": len(trace["root_spans"]),
                },
                "attributes": attributes,
            }
            # Missing output and explicit null have different upstream semantics.
            for field in ("input", "output"):
                if field in payload:
                    span[field] = payload[field]
            trace["root_spans"].append(span)
            coverage["tool_spans"] += 1
    certified, certified_coverage = _certified_traces(events, chain_id)
    coverage.update(certified_coverage)
    coverage["tool_spans"] += len(certified)
    return [
        Trace.model_validate({"aggregate": {}, **t}) for t in [*grouped.values(), *certified]
    ], coverage


def finding(kind: str, agent: str, detector: str, evidence: list[dict], *, synthetic=False) -> dict:
    return {
        "id": digest([kind, agent, detector, evidence]),
        "kind": kind,
        "agent_id": agent,
        "detector": detector,
        "evidence": evidence,
        "synthetic": synthetic,
        "severity": "review",
        "disposition": "advisory",
        "impact": "not established",
    }


def nomos_findings(events: list[dict], chain_id: str) -> list[dict]:
    groups = defaultdict(list)
    results = []
    for event in events:
        payload = event["payload"]
        agent = payload.get("agent_id")
        if not isinstance(agent, str) or not agent:
            agent = "unattributed"
        if event["entry_type"] == "verification_failed":
            results.append(
                finding(
                    "verification_failure",
                    agent,
                    "nomos-rules-v1",
                    [reference(event, chain_id)],
                    synthetic=payload.get("synthetic") is True,
                )
            )
        if event["entry_type"] != "intent_review_recorded" or payload.get("schema_version") != 1:
            continue
        if payload.get("final", {}).get("decision") not in {"deny", "denied"}:
            continue
        intent = payload.get("intent", {})
        # A missing principal/command does not establish repeated behavior by an agent.
        if agent == "unattributed" or not intent.get("command"):
            continue
        target = {
            k: intent.get(k)
            for k in (
                "command",
                "working_directory",
                "target_resource",
                "action_type",
                "target_host",
                "target_environment",
                "target_cluster",
                "target_namespace",
                "target_container",
                "parameters",
                "execution_context",
            )
        }
        key = (agent, digest(target))
        timestamp = datetime.fromisoformat(event["timestamp"])
        groups[key] = [
            prior
            for prior in groups[key]
            if 0 <= (timestamp - datetime.fromisoformat(prior["timestamp"])).total_seconds() <= 900
        ]
        groups[key].append(event)
        if len(groups[key]) >= 3:
            evidence = [reference(e, chain_id) for e in groups[key][-3:]]
            results.append(
                finding(
                    "repeated_unchanged_denial",
                    agent,
                    "nomos-rules-v1",
                    evidence,
                    synthetic=all(e["payload"].get("synthetic") is True for e in groups[key][-3:]),
                )
            )
    return results


async def analyze(events: list[dict], chain_id: str) -> tuple[list[dict], dict]:
    from insight_agent.evidence_streams.tool_issues.stream import ToolIssueEvidenceStream
    from trace_ingest.models import TraceSnapshot

    traces, coverage = project_traces(events, chain_id)
    findings = nomos_findings(events, chain_id)
    stream = ToolIssueEvidenceStream()
    snapshot = TraceSnapshot(traces)
    coverage.update(
        {
            "compass_commit": COMPASS_COMMIT,
            "compass_problems": 0,
            "coverage_measured_by_eval_author": False,
        }
    )
    if stream.check_prerequisites(snapshot) is None:
        result = await stream.analyze(snapshot)
        by_id = {t.id: t for t in traces}
        for item in result.artifacts.findings:
            trace = by_id[item["trace_id"]]
            indices = item["evidence"].get("call_indices", [])
            evidence = [
                dict(span.attributes["source_pointer"])
                for span in trace.root_spans
                if span.tool_call and span.tool_call.index in indices
            ]
            if not evidence:
                evidence = [dict(item["source_pointer"])]
            observation = finding(
                item["issue_type"],
                trace.attributes["agent_id"],
                f"compass:{item['detector_version']}:{COMPASS_COMMIT[:12]}",
                evidence,
                synthetic=trace.attributes["synthetic"],
            )
            if trace.attributes.get("correlation_scope") == "unavailable":
                # The same receipt can appear in later reviews after its first
                # review leaves the analysis window. Preserve one incident ID.
                observation["id"] = digest(
                    [
                        observation["kind"],
                        observation["agent_id"],
                        observation["detector"],
                        observation["synthetic"],
                        trace.id,
                    ]
                )
            findings.append(observation)
        coverage["limitations"] = list(result.limitations)
        coverage["compass_raw_problems"] = len(result.problems)
        coverage["compass_raw_findings"] = result.finding_count
        cards = []
        for card in result.artifacts.cards:
            members = [by_id[tid] for tid in card.trace_ids]
            # Compass defaults absent case IDs to trace IDs. Unscoped executor
            # receipts cannot establish independent cases, even across agents.
            cases = {
                t.attributes.get("logical_case_id", t.id)
                for t in members
                if t.attributes.get("correlation_scope") != "unavailable"
            }
            cards.append(
                {
                    "kind": card.issue_type,
                    "cases": len(cases),
                    "unscoped_observations": sum(
                        t.attributes.get("correlation_scope") == "unavailable" for t in members
                    ),
                    "eligible": len(cases) >= stream.config.minimum_independent_cases,
                }
            )
        coverage["recurring_cards"] = cards
        coverage["compass_problems"] = sum(card["eligible"] for card in cards)
        if certified_count := coverage["certified_evidence_tool_spans"]:
            coverage["limitations"].append(
                f"{certified_count} certified execution summaries have no recorded run/case "
                "correlation or original executor signature; retries and independent-case "
                "promotion are unavailable for these observations."
            )
    else:
        coverage["compass_skip_reason"] = "No recorded tool observations"
    return findings, coverage
