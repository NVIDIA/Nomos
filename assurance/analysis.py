# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Deterministic advisory findings. Never authorize, punish, or classify intent."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, ValidationError

EVIDENCE_ADAPTER_VERSION = 4
COMPASS_COMMIT = "518ad41d583e259205fb89623cb035ae3904f3f8"  # pragma: allowlist secret
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

    model_config = ConfigDict(strict=True)

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
                        "tool_call": {"call_id": writ_id, "index": 0},
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
                    "evidence_scopes": [],
                    "timestamp": event["timestamp"],
                },
            }
        )
    coverage["certified_evidence_tool_spans"] = len(traces)
    return traces, coverage


REQUEST_FIELDS = (
    "intent_id",
    "description",
    "justification",
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
    "context",
    "operation",
    "blast_radius",
    "preceding_commands",
)
REVIEW_FIELDS = (
    "opa",
    "policy_evaluation",
    "chairman",
    "code_review",
    "quorum",
    "debate_rounds",
    "final",
    "timing",
)


def _valid_review(event: dict, chain_id: str) -> bool:
    payload = event["payload"]
    intent = payload.get("intent")
    return (
        type(payload.get("schema_version")) is int
        and payload["schema_version"] == 1
        and isinstance(intent, dict)
        and all(
            isinstance(payload.get(key), str) and payload[key] and intent.get(key) == payload[key]
            for key in ("agent_id", "intent_id", "council_id")
        )
        and chain_id == f"ledger-{payload['council_id']}"
    )


def _context(payload: dict) -> dict:
    """Versioned signed extension, never arbitrary outer ledger metadata."""
    context = payload.get("assurance_context", {})
    return (
        context
        if isinstance(context, dict)
        and type(context.get("schema_version")) is int
        and context["schema_version"] == 1
        else {}
    )


def _conversation(context: dict) -> list[dict] | None:
    messages = context.get("messages")
    if (
        context.get("conversation_complete") is not True
        or not isinstance(messages, list)
        or not messages
    ):
        return None
    if not all(
        isinstance(m, dict)
        and m.get("role") in {"user", "assistant"}
        and isinstance(m.get("content"), str)
        for m in messages
    ):
        return None
    return [{"role": m["role"], "content": m["content"]} for m in messages]


class _RunContextConflictError(ValueError):
    """A signed run has incompatible provenance and must be excluded."""


def project_traces(events: list[dict], chain_id: str):
    from trace_ingest.models import Trace

    grouped = {}
    rejected_runs = set()
    run_span_ids = {}
    coverage = {
        "events_in_window": len(events),
        "window_limit": 2000,
        "tool_spans": 0,
        "review_spans": 0,
        "structured_tool_requests": 0,
        "unsupported_execution_events": 0,
        "correlated_execution_spans": 0,
        "conflicting_execution_context": 0,
        "attribution": "signed ledger payload; writer-asserted agent identity",
    }
    ordered = sorted(events, key=lambda e: e["sequence"])
    # Link native sandbox results using recorded token -> started intent -> review.
    # Ambiguous identities are excluded, never joined by command or timestamp.
    reviews, starts, conflicts = {}, {}, set()
    for event in ordered:
        payload = event["payload"]
        if event["entry_type"] == "intent_review_recorded" and _valid_review(event, chain_id):
            identity = payload["intent_id"]
            if identity in reviews:
                prior = reviews[identity]["payload"]
                if prior["agent_id"] != payload["agent_id"] or prior["intent"] != payload["intent"]:
                    conflicts.add(("intent", identity))
            else:
                reviews[identity] = event
        if event["entry_type"] == "execution_started" and isinstance(payload.get("token_id"), str):
            token = payload["token_id"]
            if token in starts:
                prior = starts[token]["payload"]
                if (prior.get("intent_id"), prior.get("action")) != (
                    payload.get("intent_id"),
                    payload.get("action"),
                ):
                    conflicts.add(("token", token))
            else:
                starts[token] = event
    coverage["conflicting_execution_context"] = len(conflicts)
    for event in ordered:
        payload, kind = event["payload"], event["entry_type"]
        ref = reference(event, chain_id)
        context = _context(payload)
        attributes = {"source_pointer": ref}
        if kind == "intent_review_recorded":
            tid = f"{chain_id}:review:{event['sequence']}"
            intent = payload.get("intent", {})
            if not isinstance(intent, dict):
                intent = {}
            request = {key: intent[key] for key in REQUEST_FIELDS if key in intent}
            span = {
                "id": tid,
                "kind": "GUARDRAIL",
                "input": request,
                "output": {key: payload[key] for key in REVIEW_FIELDS if key in payload},
                "attributes": {
                    **attributes,
                    "input_attribution": "agent-asserted request",
                    "output_attribution": "council review; not execution proof",
                },
            }
            scopes = ["request"] if _valid_review(event, chain_id) else []
            messages = _conversation(context)
            if messages is not None and scopes:
                span["attributes"]["messages"] = messages
                scopes.append("conversation")
            span["attributes"]["evidence_scopes"] = scopes
            grouped[tid] = {
                "id": tid,
                "root_spans": [span],
                "attributes": {
                    "agent_id": payload.get("agent_id", "unattributed"),
                    "synthetic": payload.get("synthetic") is True,
                    "evidence_scopes": scopes,
                    "timestamp": event["timestamp"],
                    "correlation_scope": "unavailable",
                },
            }
            coverage["review_spans"] += 1
            if (
                isinstance(intent.get("operation"), dict)
                and intent["operation"].get("kind") == "tool_call"
            ):
                # Nomos intentionally persists only tool name, argument digest
                # and subject. Authorization is not evidence the tool executed.
                coverage["structured_tool_requests"] += 1
        elif kind in {"execution_started", "execution_completed", "execution_failed"}:
            if kind == "execution_started":
                continue
            try:
                if (
                    type(payload.get("assurance_schema")) is not int
                    or payload["assurance_schema"] != 1
                ):
                    raise ValueError("no complete signed observation")
                observation = ToolObservation.model_validate(payload)
            except (ValidationError, ValueError):
                # Native executor terminal events lack agent identity. Use only
                # the signed chain's explicit started-token and reviewed-intent links.
                token = payload.get("token_id")
                start = starts.get(token) if isinstance(token, str) else None
                intent_id = start["payload"].get("intent_id") if start else None
                review = reviews.get(intent_id) if isinstance(intent_id, str) else None
                if (
                    not start
                    or not review
                    or ("token", token) in conflicts
                    or ("intent", intent_id) in conflicts
                    or not review["sequence"] < start["sequence"] < event["sequence"]
                ):
                    coverage["unsupported_execution_events"] += 1
                    continue
                tid = f"{chain_id}:execution:{digest(token)[:24]}"
                agent = review["payload"]["agent_id"]
                span = {
                    "id": f"{chain_id}:{event['sequence']}",
                    "kind": "TOOL",
                    "tool_name": payload.get("action_type", "nomos.execute"),
                    "tool_call": {"call_id": token, "index": 0},
                    "input": start["payload"].get("action", {}),
                    "attributes": {
                        **attributes,
                        "related_sources": [
                            reference(start, chain_id),
                            reference(review, chain_id),
                        ],
                        "attribution": "signed executor outcome, explicitly linked intent",
                        "evidence_scopes": ["execution"],
                    },
                }
                for field in ("output", "error"):
                    if field in payload:
                        span[field] = payload[field]
                trace = grouped.setdefault(
                    tid,
                    {
                        "id": tid,
                        "root_spans": [],
                        "attributes": {
                            "agent_id": agent,
                            "synthetic": payload.get("synthetic") is True,
                            "logical_case_id": f"unscoped:{digest(agent)[:24]}",
                            "correlation_scope": "unavailable",
                            "evidence_scopes": ["execution"],
                            "timestamp": event["timestamp"],
                        },
                    },
                )
                trace["root_spans"].append(span)
                coverage["correlated_execution_spans"] += 1
                coverage["tool_spans"] += 1
                continue
            tid = f"{chain_id}:run:{digest([observation.agent_id, observation.run_id])[:24]}"
            if tid in rejected_runs:
                coverage["unsupported_execution_events"] += 1
                continue
            try:
                trace = dict(
                    grouped.get(
                        tid,
                        {
                            "id": tid,
                            "root_spans": [],
                            "attributes": {
                                "agent_id": observation.agent_id,
                                "logical_case_id": digest(
                                    [chain_id, observation.agent_id, observation.logical_case_id]
                                ),
                                "synthetic": payload.get("synthetic") is True,
                                "correlation_scope": "signed run and case identifiers",
                                "evidence_scopes": ["execution"],
                                "timestamp": event["timestamp"],
                            },
                        },
                    )
                )
                trace["attributes"] = dict(trace["attributes"])
                if trace["attributes"]["logical_case_id"] != digest(
                    [chain_id, observation.agent_id, observation.logical_case_id]
                ) or trace["attributes"]["synthetic"] != (payload.get("synthetic") is True):
                    raise _RunContextConflictError("conflicting signed run context")
                tool_call = {"call_id": observation.call_id, "index": len(trace["root_spans"])}
                for key in (
                    "result_id",
                    "result_count",
                    "instrumentation_alias_of",
                    "prior_user_text",
                    "returned_data",
                ):
                    if key in payload:
                        tool_call[key] = payload[key]
                span = {
                    "id": f"{chain_id}:{event['sequence']}",
                    "kind": "TOOL",
                    "tool_name": observation.tool_name,
                    "tool_call": tool_call,
                    "attributes": attributes,
                }
                for field in ("input", "output", "error"):
                    if field in payload:
                        span[field] = payload[field]
                attributes["evidence_scopes"] = ["execution"]
                if "explicit_error" in payload:
                    attributes["explicit_error"] = payload["explicit_error"]
                messages = _conversation(context)
                user_text = (
                    "\n".join(m["content"] for m in messages if m["role"] == "user")
                    if messages
                    else ""
                )
                if messages is not None:
                    tool_call["prior_user_text"] = user_text
                if "children" in context:
                    # Canonical nested spans are allowed only in this signed, versioned
                    # extension; bind every nested span to its enclosing ledger entry.
                    def bind(children, source, grounding):
                        if not isinstance(children, list):
                            raise ValueError("invalid signed children")
                        bound = []
                        for child in children:
                            if not isinstance(child, dict):
                                raise ValueError("invalid signed child span")
                            item = dict(child)
                            if not isinstance(item.get("attributes", {}), dict):
                                raise ValueError("invalid signed child attributes")
                            item["attributes"] = {
                                **item.get("attributes", {}),
                                "source_pointer": source,
                            }
                            if grounding is not None and item.get("kind") == "TOOL":
                                details = item.get("tool_call")
                                item["tool_call"] = {
                                    **({} if details is None else details),
                                    "prior_user_text": grounding,
                                }
                            item["children"] = bind(item.get("children", []), source, grounding)
                            bound.append(item)
                        return bound

                    span["children"] = bind(
                        context["children"], ref, user_text if messages is not None else None
                    )
                for field in ("tool_catalog",):
                    if field in context:
                        existing = trace["attributes"].get(field)
                        if existing is not None and existing != context[field]:
                            raise _RunContextConflictError(f"conflicting signed {field} within run")
                        trace["attributes"][field] = context[field]
                if "orphan_results" in context:
                    orphans = context["orphan_results"]
                    if not isinstance(orphans, list) or any(
                        not isinstance(o, dict) for o in orphans
                    ):
                        raise ValueError("invalid signed orphan results")
                    bound = [
                        {
                            "result_id": orphan.get("result_id"),
                            **ref,
                            "payload_path": f"/assurance_context/orphan_results/{index}",
                        }
                        for index, orphan in enumerate(orphans)
                    ]
                    trace["attributes"]["orphan_results"] = [
                        *trace["attributes"].get("orphan_results", []),
                        *bound,
                    ]
                if messages is not None:
                    attributes["messages"] = messages
                    attributes["evidence_scopes"] = ["execution", "conversation"]
                    trace["attributes"]["evidence_scopes"] = ["execution", "conversation"]
                # Validate just this record before committing it. Revalidating or
                # copying the growing run on every append makes full windows quadratic.
                projected = Trace.model_validate({"aggregate": {}, **trace, "root_spans": [span]})
                pending = list(projected.root_spans)
                span_ids = set()
                while pending:
                    child = pending.pop()
                    span_ids.add(child.id)
                    pending.extend(child.children)
                prior_ids = run_span_ids.get(tid, set())
                if span_ids & prior_ids:
                    raise ValueError("duplicate signed span id within run")
                # A complete captured conversation for one observation cannot prove
                # that other calls in the same run have complete grounding.
                trace["attributes"]["complete_provenance_context"] = (
                    trace["attributes"].get("complete_provenance_context", True)
                    and messages is not None
                )
                trace["root_spans"].append(span)
                prior_ids.update(span_ids)
                run_span_ids[tid] = prior_ids
                grouped[tid] = trace
            except _RunContextConflictError:
                prior = grouped.pop(tid, None)
                coverage["unsupported_execution_events"] += 1 + (
                    len(prior["root_spans"]) if prior else 0
                )
                coverage["conflicting_execution_context"] += 1
                rejected_runs.add(tid)
            except (ValueError, TypeError, RecursionError):
                # Commit only validated spans/metadata so earlier records remain intact.
                coverage["unsupported_execution_events"] += 1
    certified, certified_coverage = _certified_traces(events, chain_id)
    coverage.update(certified_coverage)
    traces = []
    for trace in [*grouped.values(), *certified]:
        try:
            traces.append(Trace.model_validate({"aggregate": {}, **trace}))
        except (ValueError, TypeError):
            coverage["unsupported_execution_events"] += len(trace["root_spans"])
    coverage["tool_spans"] = sum(
        span.kind.value == "TOOL" for trace in traces for span in trace.root_spans
    )
    coverage["review_spans"] = sum(
        span.kind.value == "GUARDRAIL" for trace in traces for span in trace.root_spans
    )
    return traces, coverage


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
        final = payload.get("final")
        if not isinstance(final, dict) or final.get("decision") not in {"deny", "denied"}:
            continue
        intent = payload.get("intent")
        if not isinstance(intent, dict):
            continue
        # A missing principal/command does not establish repeated behavior by an agent.
        if agent == "unattributed" or not (intent.get("command") or intent.get("operation")):
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
                "operation",
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
    from insight_agent.compass_signals.tool_issues.coverage import corpus_coverage
    from insight_agent.compass_signals.tool_issues.signal import (
        ToolIssueCompassSignal,
        to_tool_issue_trace,
    )
    from trace_ingest.models import TraceSnapshot

    traces, coverage = project_traces(events, chain_id)
    findings = nomos_findings(events, chain_id)
    stream = ToolIssueCompassSignal()
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
        hashes = {event["sequence"]: event["content_hash"] for event in events}
        for item in result.artifacts.findings:
            trace = by_id[item["trace_id"]]
            indices = item["evidence"].get("call_indices", [])
            evidence = [
                dict(call.source_pointer)
                for call in to_tool_issue_trace(trace).calls
                if call.call_index in indices
            ]
            if not evidence:
                evidence = [dict(item["source_pointer"])]
            if any(
                ref.get("chain_id") != chain_id
                or type(ref.get("sequence")) is not int
                or hashes.get(ref["sequence"]) != ref.get("content_hash")
                for ref in evidence
            ):
                raise ValueError("Compass finding has no verified ledger reference")
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
            observation["stream"] = "tool-issues"
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

    class ProjectedLoader:
        def load(self):
            return snapshot

        def describe(self):
            return {"source": "verified-nomos-ledger"}

    coverage["tool_rule_coverage"] = corpus_coverage(
        ProjectedLoader(), findings=result.artifacts.findings if "result" in locals() else []
    )
    for item in findings:
        item.setdefault("stream", "tool-issues")
    return findings, coverage
