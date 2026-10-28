# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Private, bounded evaluation authoring captures. No authoring agent runs here."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from collections import defaultdict
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from assurance.analysis import (
    AUTHOR_COMMIT,
    EVIDENCE_ADAPTER_VERSION,
    REQUEST_FIELDS,
    REVIEW_FIELDS,
    digest,
    project_traces,
    reference,
)
from assurance.monitor import Store

SCHEMA = "nomos.evaluation-authoring.v1"
PROFILE = "private-operator-v1"
MAX_FINDINGS = 20
MAX_RECORDS = 2000
MAX_BYTES = 8 * 1024 * 1024
MAX_RUNS = 20
TTL_SECONDS = 600
IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$"
_SECRET_KEY = re.compile(
    r"(?i)(password|passwd|secret|authorization|cookie|api[_-]?key|private[_-]?key|"
    r"token|credential|capability|writ|signature)"
)
_SECRET_TEXT = re.compile(
    r"(?is)-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
    r"|\bauthorization\s*:\s*(?:Bearer|Basic)\s+[^\s\"'<>]+"
    r"|\b(?:Bearer|Basic)\s+[^\s\"'<>]+"
    r"|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r"|\b(?:Cookie|Set-Cookie)\s*:\s*[^\r\n]+"
    r"|\b(?:sk-|nvapi-)[A-Za-z0-9_-]{16,}"
    r"|\b(?:password|passwd|secret|token|api[_-]?key|authorization)\s*[:=]\s*[^\s,;]+"
    r"|https?://[^\s/@:]+:[^\s/@]+@[^\s]+"
)
FINDING_FIELDS = {
    "id",
    "kind",
    "stream",
    "agent_id",
    "detector",
    "evidence",
    "run_id",
    "ethos",
    "disposition",
    "explanation",
    "synthetic",
    "created_at",
    "history",
}
RUN_FIELDS = {
    "id",
    "stream",
    "config_digest",
    "sequence_start",
    "sequence_end",
    "evidence_start",
    "checkpoint",
    "evidence_digest",
    "status",
    "created_at",
    "completed_at",
    "coverage",
    "export_context_role",
}
PAYLOAD_FIELDS = {
    "schema_version",
    "assurance_schema",
    "agent_id",
    "intent_id",
    "council_id",
    "run_id",
    "logical_case_id",
    "tool_name",
    "call_id",
    "result_id",
    "success",
    "exit_code",
    "input",
    "output",
    "error",
    "reason",
    "final",
    "action_type",
    "synthetic",
    "assurance_context",
    "token_id",
    "action",
    "intent",
    *REVIEW_FIELDS,
}
CONTEXT_FIELDS = {
    "schema_version",
    "conversation_complete",
    "messages",
    "tool_catalog",
    "environment",
    "messages_position",
}


# Exact deterministic preparation scripts from the reviewed Eval Author revision.
VALIDATOR_HASHES = {
    "trace_environment.py": (
        "586ba1235bc8cceee94a645d6a00aff1"  # pragma: allowlist secret
        "79e09edf5b3adbda47e39b114ea45fd2"  # pragma: allowlist secret
    ),
    "fixture_compiler.py": (
        "eacc395434eae316fb9b9dd24dcf4442"  # pragma: allowlist secret
        "def746ba3e5a473dd593c2ac3bcc69f5"  # pragma: allowlist secret
    ),
    "mock_registration.py": (
        "af272e2bf0ba6c5e7a499ef546f3c712"  # pragma: allowlist secret
        "55b4def6c84c1b488256cf52a80d88ed"  # pragma: allowlist secret
    ),
}


def encoded(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, indent=2).encode()


class ExportError(ValueError):
    """Public errors have fixed messages and never include source contents."""


class Redactor:
    def __init__(self, secrets: list[str]):
        self.secrets = sorted(set(filter(None, secrets)), key=len, reverse=True)
        self.changes: list[dict] = []

    def clean(self, value, path="", depth=0):
        if depth > 24:
            self.changes.append({"path_hash": digest(path), "reason": "depth_limit"})
            return "[OMITTED]"
        if isinstance(value, str):
            original = value
            for secret in self.secrets:
                value = value.replace(secret, "[REDACTED]")
            value = _SECRET_TEXT.sub("[REDACTED]", value)
            if len(value) > 65536:
                value = value[:65536] + "[TRUNCATED]"
            if original != value:
                self.changes.append(
                    {"path_hash": digest(path), "reason": "content_redacted_or_truncated"}
                )
            return value
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                location = path + "/" + str(key)
                # Identity hashes of writs/tokens may link evidence; reusable bytes never leave.
                if _SECRET_KEY.search(str(key)) or key in {"token_id", "nonce"}:
                    result[self.clean(str(key))] = {"identity_sha256": digest(item)}
                    self.changes.append(
                        {"path_hash": digest(location), "reason": "sensitive_field_omitted"}
                    )
                else:
                    result[self.clean(str(key))] = self.clean(item, location, depth + 1)
            return result
        if isinstance(value, list):
            return [self.clean(item, path + f"/{i}", depth + 1) for i, item in enumerate(value)]
        return value


def _load(db, table, key, value, omissions, budget=None):
    # Table and column names are internal constants, never request inputs.
    row = db.execute(
        f"SELECT length(body), CASE WHEN length(body)<=? THEN body END "  # noqa: S608  # nosec B608
        f"FROM {table} WHERE {key}=?",
        (MAX_BYTES, value),
    ).fetchone()
    if not row:
        return None
    if row[0] > MAX_BYTES:
        omissions.append(
            {"reason": "source_object_size_limit", "source_identity": digest([table, value])}
        )
        return None
    if budget is not None:
        if row[0] > budget["remaining"]:
            omissions.append(
                {"reason": "source_byte_budget", "source_identity": digest([table, value])}
            )
            return None
        budget["remaining"] -= row[0]
    return json.loads(row[1])


def _relations(payload: dict) -> list[tuple]:
    result = [
        (key, payload[key])
        for key in ("intent_id", "token_id")
        if isinstance(payload.get(key), str) and payload[key]
    ]
    scope = tuple(payload.get(k) for k in ("agent_id", "run_id", "logical_case_id"))
    if all(isinstance(value, str) and value for value in scope):
        result.append(("case", *scope))
    return result


def resolve_selection(store: Store, finding_ids: list[str]) -> dict:
    """One SQLite read transaction; no latest-window substitution or fuzzy joins."""
    if not 1 <= len(finding_ids) <= MAX_FINDINGS or len(set(finding_ids)) != len(finding_ids):
        raise ExportError("Select 1 to 20 distinct finding IDs")
    if any(not re.fullmatch(IDENTIFIER, item) for item in finding_ids):
        raise ExportError("Invalid finding ID")
    findings, runs, records, omissions, references = [], {}, {}, [], []
    budget = {"remaining": MAX_BYTES}
    with closing(store.connect()) as db:
        db.execute("BEGIN")
        for finding_id in finding_ids:
            finding = _load(db, "findings", "id", finding_id, omissions)
            if finding is None:
                raise ExportError("Selected finding is unavailable or exceeds capture limits")
            history = [
                r[0]
                for r in db.execute(
                    "SELECT run_id FROM run_findings WHERE finding_id=? ORDER BY rowid LIMIT ?",
                    (finding_id, MAX_RUNS + 1),
                )
            ]
            finding["history"] = history[:MAX_RUNS]
            findings.append(finding)
            run_id = finding.get("run_id")
            # The originating analysis snapshot determines this finding's source scope.
            # History is attribution, not permission to pull every subsequent analysis window.
            run = runs.get(run_id)
            if run_id and run is None:
                run = _load(db, "runs", "id", run_id, omissions, budget)
                if run:
                    runs[run_id] = run
            snapshot = run.get("input_snapshot", []) if run else []
            if len(snapshot) > MAX_RECORDS:
                omissions.append({"finding_id": finding_id, "reason": "run_snapshot_record_limit"})
                snapshot = snapshot[:MAX_RECORDS]
            if not run:
                omissions.append({"finding_id": finding_id, "reason": "historical_run_unavailable"})
            if len(history) > MAX_RUNS:
                omissions.append({"finding_id": finding_id, "reason": "run_history_limit"})
            indexed = {e.get("sequence"): e for e in snapshot}
            snapshot_relations = defaultdict(list)
            for source in snapshot:
                for relation in _relations(source.get("payload", {})):
                    snapshot_relations[relation].append(source)
            selected = {}
            for ref in finding.get("evidence", []):
                found = None
                if ref.get("chain_id") == store.chain_id and type(ref.get("sequence")) is int:
                    found = indexed.get(ref["sequence"])
                    if not found or found.get("content_hash") != ref.get("content_hash"):
                        found = _load(db, "events", "sequence", ref["sequence"], omissions, budget)
                    if found and found.get("content_hash") != ref.get("content_hash"):
                        found = None
                references.append(
                    {"finding_id": finding_id, "reference": ref, "resolved": bool(found)}
                )
                if found:
                    selected[found["sequence"]] = found
                else:
                    omissions.append(
                        {"finding_id": finding_id, "reason": "referenced_record_unavailable"}
                    )
            # Expand only explicit signed relationships, including retained records beyond the run.
            # A limited number of hops covers terminal -> start -> intent -> terminal.
            queried = set()
            for _ in range(3):
                previous = set(selected)
                for event in list(selected.values()):
                    p = event.get("payload", {})
                    clauses, values = [], []
                    for field in ("intent_id", "token_id"):
                        if isinstance(p.get(field), str) and p[field]:
                            clauses.append(f"json_extract(body, '$.payload.{field}')=?")
                            values.append(p[field])
                    if all(
                        isinstance(p.get(k), str) and p[k]
                        for k in ("agent_id", "run_id", "logical_case_id")
                    ):
                        clauses.append(
                            "(json_extract(body, '$.payload.agent_id')=? AND "
                            "json_extract(body, '$.payload.run_id')=? AND "
                            "json_extract(body, '$.payload.logical_case_id')=?)"
                        )
                        values.extend(p[k] for k in ("agent_id", "run_id", "logical_case_id"))
                    if not clauses:
                        continue
                    query_identity = (tuple(clauses), tuple(values))
                    if query_identity in queried:
                        continue
                    queried.add(query_identity)
                    rows = db.execute(
                        "SELECT sequence, hash, length(body) FROM events WHERE ("  # noqa: S608  # nosec B608
                        + " OR ".join(clauses)
                        + ") ORDER BY sequence LIMIT ?",
                        (*values, MAX_RECORDS + 1),
                    ).fetchall()
                    if len(rows) > MAX_RECORDS:
                        omissions.append(
                            {"finding_id": finding_id, "reason": "related_record_limit"}
                        )
                    for sequence, source_hash, size in rows[:MAX_RECORDS]:
                        if sequence in selected:
                            continue
                        historical = indexed.get(sequence)
                        if historical and historical.get("content_hash") == source_hash:
                            selected[sequence] = historical
                            continue
                        if size > budget["remaining"]:
                            omissions.append(
                                {"finding_id": finding_id, "reason": "related_source_size_limit"}
                            )
                            continue
                        related = _load(db, "events", "sequence", sequence, omissions, budget)
                        if related:
                            selected[sequence] = related
                    # Historical snapshots survive event retention. Index explicit
                    # relationships once instead of repeatedly scanning the window.
                    for relation in _relations(p):
                        for related in snapshot_relations[relation]:
                            selected.setdefault(related["sequence"], related)
                if previous == set(selected):
                    break
            if len({k[0] for k in records} | selected.keys()) > MAX_RECORDS:
                omissions.append({"finding_id": finding_id, "reason": "capture_record_limit"})
            for sequence, event in sorted(selected.items()):
                key = (sequence, event["content_hash"])
                if key not in records and len(records) < MAX_RECORDS:
                    records[key] = event
            finding["capture_records"] = [
                reference(e, store.chain_id)
                for e in selected.values()
                if (e["sequence"], e["content_hash"]) in records
            ]
        wanted = set(records)
        if wanted:
            lower, upper = min(k[0] for k in wanted), max(k[0] for k in wanted)
            context_rows = db.execute(
                "SELECT id, length(body) FROM runs "
                "WHERE stream='ethos-divergence' AND sequence_end>=? "
                "AND COALESCE(json_extract(body, '$.evidence_start'), 0)<=? "
                "ORDER BY rowid DESC LIMIT ?",
                (lower, upper, MAX_RUNS + 1),
            ).fetchall()
            if len(context_rows) > MAX_RUNS:
                omissions.append({"reason": "ethos_context_run_limit"})
            for context_id, size in context_rows[:MAX_RUNS]:
                if context_id in runs:
                    continue
                if size > budget["remaining"]:
                    omissions.append({"reason": "ethos_context_size_limit"})
                    continue
                context_run = _load(db, "runs", "id", context_id, omissions, budget)
                if context_run is None:
                    continue
                if context_run["id"] in runs:
                    continue
                exact = {
                    (e.get("sequence"), e.get("content_hash"))
                    for e in context_run.get("input_snapshot", [])
                }
                if wanted & exact:
                    context_run["export_context_role"] = "shares_exact_selected_evidence"
                    runs[context_run["id"]] = context_run
        frozen = {
            "findings": findings,
            "runs": list(runs.values()),
            "records": sorted(records.values(), key=lambda e: e["sequence"]),
            "references": references,
            "omissions": omissions,
            "chain_id": store.chain_id,
        }
        if len(encoded(frozen)) > MAX_BYTES:
            raise ExportError(
                "Selected evidence exceeds the 8 MiB capture limit; select fewer findings"
            )
        return frozen


def _record(event: dict, chain_id: str) -> dict:
    payload = {k: v for k, v in event.get("payload", {}).items() if k in PAYLOAD_FIELDS}
    if isinstance(payload.get("intent"), dict):
        intent = payload["intent"]
        payload["intent"] = {
            k: v for k, v in intent.items() if k in {*REQUEST_FIELDS, "agent_id", "council_id"}
        }
        # Certified receipts are useful summaries, never reusable signed capabilities.
        metadata = intent.get("metadata")
        receipts = metadata.get("certified_evidence", []) if isinstance(metadata, dict) else []
        if isinstance(receipts, list) and receipts:
            fields = {
                "executor_id",
                "executor_key_fingerprint",
                "writ_id",
                "command",
                "success",
                "exit_code",
                "stdout_sha256",
                "stderr_sha256",
            }
            unique = {
                digest(r): {k: v for k, v in r.items() if k in fields}
                for r in receipts
                if isinstance(r, dict)
            }
            payload["certified_receipt_summaries"] = list(unique.values())
    if isinstance(payload.get("assurance_context"), dict):
        payload["assurance_context"] = {
            k: v for k, v in payload["assurance_context"].items() if k in CONTEXT_FIELDS
        }
    return {
        "source": reference(event, chain_id),
        "timestamp": event.get("timestamp"),
        "entry_type": event.get("entry_type"),
        "payload": payload,
        "attribution": "transformed signed-ledger content; original signatures omitted",
    }


def _atif(events: list[dict], chain_id: str) -> tuple[dict | None, str]:
    """Narrow client path: one explicitly scoped run with identical complete pre-call dialogue."""
    observations = [
        e for e in events if e.get("entry_type") in {"execution_completed", "execution_failed"}
    ]
    if not observations:
        return None, "No recorded client tool interactions"
    p = observations[0].get("payload", {})
    identity = [p.get(k) for k in ("agent_id", "run_id", "logical_case_id")]
    context = p.get("assurance_context", {})
    messages = context.get("messages") if isinstance(context, dict) else None
    if (
        not isinstance(context, dict)
        or not all(isinstance(i, str) and i for i in identity)
        or type(context.get("schema_version")) is not int
        or context.get("schema_version") != 1
        or context.get("conversation_complete") is not True
        or context.get("messages_position") != "before_tools"
        or not isinstance(messages, list)
        or not messages
        or not any(
            m.get("role") == "user" and m.get("content") for m in messages if isinstance(m, dict)
        )
        or not all(
            isinstance(m, dict)
            and m.get("role") in {"user", "assistant"}
            and isinstance(m.get("content"), str)
            for m in messages
        )
    ):
        return (
            None,
            "Original complete user/assistant conversation and signed run/case identities required",
        )
    steps = [
        {
            "step_id": i,
            "source": "user" if m["role"] == "user" else "agent",
            "message": m["content"],
        }
        for i, m in enumerate(messages, 1)
    ]
    calls = set()
    for event in observations:
        q = event.get("payload", {})
        if (
            type(q.get("assurance_schema")) is not int
            or q.get("assurance_schema") != 1
            or [q.get(k) for k in ("agent_id", "run_id", "logical_case_id")] != identity
            or q.get("assurance_context") != context
            or context.get("children")
            or context.get("orphan_results")
            or not isinstance(q.get("call_id"), str)
            or not q["call_id"]
            or q["call_id"] in calls
            or q.get("result_id", q["call_id"]) != q["call_id"]
            or q.get("result_count", 1) != 1
            or not isinstance(q.get("tool_name"), str)
            or not q["tool_name"]
            or not isinstance(q.get("input"), dict)
            or "output" not in q
        ):
            return (
                None,
                "Conflicting/partial run, missing arguments/results, nested or duplicate calls; "
                "manual review required",
            )
        calls.add(q["call_id"])
        steps.append(
            {
                "step_id": len(steps) + 1,
                "source": "agent",
                "message": "",
                "tool_calls": [
                    {
                        "tool_call_id": q["call_id"],
                        "function_name": q["tool_name"],
                        "arguments": q["input"],
                    }
                ],
                "observation": {
                    "results": [
                        {"source_call_id": q["call_id"], "content": json.dumps(q["output"])}
                    ]
                },
                "extra": {
                    "source": reference(event, chain_id),
                    "timestamp": event.get("timestamp"),
                    "message_absent": True,
                },
            }
        )
    return {
        "schema_version": "ATIF-v1.0",
        "session_id": digest([chain_id, *identity]),
        "agent": {
            "name": p["agent_id"],
            "version": "unrecorded",
            "extra": {"version_recorded": False},
        },
        "steps": steps,
        "extra": {
            "normalization": {
                "losses": [
                    "No post-tool assistant answer recorded",
                    "Agent version was not recorded; version=unrecorded is an absence marker",
                    "Tool definitions/environment are separate evidence when recorded",
                ],
                "uncertainties": [
                    "conversation_complete is a signed source assertion; "
                    "task ground truth needs review"
                ],
            },
            "run_id": p["run_id"],
            "logical_case_id": p["logical_case_id"],
        },
    }, ""


def _validate_atif(trace: dict) -> dict:
    helper = (
        Path(os.environ.get("EVAL_AUTHOR_ROOT", "/opt/eval-author"))
        / "skills/eval-author-trace-environment/scripts/trace_environment.py"
    )
    if not helper.is_file():
        raise ExportError("Pinned Eval Author trace preparation tooling unavailable")
    for name, expected in VALIDATOR_HASHES.items():
        if hashlib.sha256((helper.parent / name).read_bytes()).hexdigest() != expected:
            raise ExportError("Eval Author trace validator differs from the pinned revision")
    with tempfile.TemporaryDirectory(prefix="nomos-atif-") as directory:
        source, task = (
            Path(directory, "source.json"),
            Path(directory, ".eval-author/tasks/nomos-capture"),
        )
        source.write_bytes(encoded(trace))
        commands = [
            ["init", "--task-id", "nomos-capture", "--root", str(task.parent)],
            ["prepare", "--task-dir", str(task), "--atif", str(source), "--source-kind", "atif"],
        ]
        for command in commands:
            result = subprocess.run(
                [sys.executable, str(helper), *command],
                capture_output=True,
                timeout=5,
                check=False,
            )
            if result.returncode:
                raise ExportError("Pinned Eval Author rejected the converted trace")
        return {
            "validator": "eval-author trace_environment.py prepare",
            "eval_author_commit": AUTHOR_COMMIT,
            "valid": True,
            "validator_sha256": VALIDATOR_HASHES,
            "validated_sha256": hashlib.sha256(encoded(trace)).hexdigest(),
            "task_proof": "not_run",
        }


README = """# Private Nomos evaluation authoring bundle

Review this private artifact before sharing. Automated redaction is bounded and
cannot establish that arbitrary natural-language content is safe to publish.
`manifest.json` inventories exported bytes; `evidence/records.jsonl` is transformed
ledger content, NOT an original signed payload. Source hashes identify originals;
export hashes describe different bytes. Original signatures/capabilities are absent.

Start with `completeness.json`: each selected finding is a separate candidate.
Missing historical records are never replaced by recent unrelated activity.
Council approval is not execution success; receipt output hashes are not output.
Historical ETHOS files retain original source hashes and mapping provenance;
redacted documents are explicitly transformed and require semantic review.
No root ETHOS or generic tool catalog is invented. Shared source records are deduplicated.

## Offline toolchain and supported routes

Tested Eval Author revision: 20f95a0a010d4905c13065678af51970404910c9.
The deployment includes only deterministic validation scripts, not an authoring
agent, Harbor, Docker task images, or model credentials.

Council-only captures use Eval Author's repository-based `eval-author-first-eval`
workflow. They are NOT native ATIF and must not be placed in a trace input directory.
Open a private recipient repository with Eval Author at the pinned revision, give
it this extracted bundle as the explicit evidence corpus, and ask:

> Use Eval Author to build one private Harbor evaluation case for the selected
> council decision finding. Read completeness.json, findings.json, runs.json,
> evidence/ and the applicable historical ethos/ documents. Preserve the distinction
> between recorded evidence and proposed fixture inputs. Record missing contract,
> policy, council code/model configuration and environment prerequisites before
> scaffolding. Follow eval-author-first-eval; do not invent the original user goal.

Only files listed under `validation.json.traces` passed the pinned ATIF preparation
command. For such a file, use the upstream trace-to-environment workflow:

> Use Eval Author to derive a private Harbor task from /absolute/path/traces/FILE.atif.json.
> Review privacy, ground truth and conversion losses, then validate the candidate
> when its required runtime is available.

You supply your agent repository/runtime, applicable contract if absent, tool
schemas if absent, model access and any initial world state Nomos never recorded.
Do not treat council explanations as client dialogue. Do not infer commands or
results from digests. Review redactions and supply missing inputs privately with
explicit external provenance; a supplement is not historical ledger evidence.

Review the authored instruction and verifier against the applicable contract.
Run Harbor oracle, nop and a realistic targeted incorrect control. Verify reward
1 for the correct solution and 0 for both failure controls, with no runtime errors.
Task proof and trace preparation are separate from measured agent performance.
Then run the real agent with your configured provider; retain execution traces,
receipts and task hashes before comparing a baseline and candidate.

The reproducible council/manual pilot, exact offline commands, runtime pin and
proof limitations are in Nomos docs/assurance/EVALUATION.md. Export does not run
an optimizer, change permissions, or establish production evaluation coverage.
"""


def _agent_attribution(events: list[dict], chain_id: str) -> dict:
    """Use the analyzer's supported identity links, including native sandbox events."""
    traces, _ = project_traces(events, chain_id)
    result = {}
    for trace in traces:
        agent = trace.attributes.get("agent_id")
        if not agent or agent == "unattributed":
            continue
        for span in trace.root_spans:
            source = span.attributes.get("source_pointer", {})
            if not source or not set(span.attributes.get("evidence_scopes", [])) & {
                "request",
                "execution",
            }:
                continue
            identity = (source.get("sequence"), source.get("content_hash"))
            result[identity] = {
                "agent_id": agent,
                "adapter_version": EVIDENCE_ADAPTER_VERSION,
                "basis": "same signed-payload and explicit intent/token links as analysis",
                "related_sources": span.attributes.get("related_sources", []),
            }
    return result


def build_capture(frozen: dict, subject: str, secrets: list[str]) -> tuple[bytes, dict]:
    if subject not in {"council-decision", "client-agent"}:
        raise ExportError("Unsupported evaluation subject")
    redactor = Redactor(secrets)
    attribution = _agent_attribution(frozen["records"], frozen["chain_id"])
    files = {"README.md": README.encode()}
    findings = [
        {k: v for k, v in f.items() if k in FINDING_FIELDS | {"capture_records"}}
        for f in frozen["findings"]
    ]
    files["findings.json"] = encoded(redactor.clean(findings, "findings"))
    files["runs.json"] = encoded(
        redactor.clean(
            [{k: v for k, v in r.items() if k in RUN_FIELDS} for r in frozen["runs"]], "runs"
        )
    )
    transformed, receipts, tools, environments = [], {}, [], []
    for event in frozen["records"]:
        record = _record(event, frozen["chain_id"])
        identity = (event["sequence"], event["content_hash"])
        if identity in attribution:
            record["agent_attribution"] = attribution[identity]
        summaries = record["payload"].pop("certified_receipt_summaries", [])
        summary_refs = []
        for summary in summaries:
            receipt_id = digest(summary.get("writ_id"))
            item = receipts.setdefault(
                receipt_id,
                {
                    "receipt_identity_sha256": receipt_id,
                    "sources": [],
                    "variants": {},
                    "attribution": (
                        "council-validated receipt summary; "
                        "original signature and output bytes absent"
                    ),
                },
            )
            item["sources"].append(record["source"])
            item["variants"][digest(summary)] = summary
            summary_refs.append(receipt_id)
        if summary_refs:
            record["payload"]["certified_receipt_references"] = sorted(set(summary_refs))
        context = record["payload"].get("assurance_context", {})
        if isinstance(context, dict):
            if context.get("tool_catalog"):
                tools.append({"source": record["source"], "definitions": context["tool_catalog"]})
            if context.get("environment"):
                environments.append(
                    {"source": record["source"], "description": context["environment"]}
                )
        record["omitted_payload_fields"] = sorted(set(event.get("payload", {})) - PAYLOAD_FIELDS)
        transformed.append(redactor.clean(record, f"record/{event['sequence']}"))
    if receipts:
        for item in receipts.values():
            item["conflicting_summaries"] = len(item["variants"]) > 1
            item["variants"] = list(item["variants"].values())
        files["context/receipt-summaries.json"] = encoded(
            redactor.clean(list(receipts.values()), "receipts")
        )
    if tools:
        files["context/tools.json"] = encoded(redactor.clean(tools, "tools"))
    if environments:
        files["context/environment.json"] = encoded(redactor.clean(environments, "environment"))
    files["evidence/records.jsonl"] = b"".join(
        encoded(e).replace(b"\n", b" ") + b"\n" for e in transformed
    )
    files["evidence/references.json"] = encoded(redactor.clean(frozen["references"], "references"))
    safe_metadata = redactor.clean(
        {
            "selected_finding_ids": [f["id"] for f in frozen["findings"]],
            "chain_id": frozen["chain_id"],
            "run_ids": [r["id"] for r in frozen["runs"]],
            "omissions": frozen["omissions"],
        },
        "manifest-source",
    )
    documents = []
    for run in frozen["runs"]:
        artifacts = run.get("ethos_artifacts") or {}
        selected_agents = {f.get("agent_id") for f in frozen["findings"]}
        registry = artifacts.get("manifest", {})
        mapping = {
            agent: assigned
            for agent, assigned in registry.get("agents", {}).items()
            if agent in selected_agents
        }
        assigned = {key for keys in mapping.values() for key in keys}
        for document in registry.get("documents", []):
            doc_key = f"{document.get('document_id')}@{document.get('version')}"
            if doc_key not in assigned:
                continue
            applicable = []
            for event in frozen["records"]:
                agent = attribution.get((event["sequence"], event["content_hash"]), {}).get(
                    "agent_id", event.get("payload", {}).get("agent_id")
                )
                if doc_key not in mapping.get(agent, []):
                    continue
                when = event.get("timestamp")
                start, end = document.get("effective_from"), document.get("effective_until")
                if (
                    when
                    and (not start or datetime.fromisoformat(when) >= datetime.fromisoformat(start))
                    and (not end or datetime.fromisoformat(when) < datetime.fromisoformat(end))
                ):
                    applicable.append(reference(event, frozen["chain_id"]))
            if not applicable:
                continue
            name = document.get("filename")
            content = artifacts.get("documents", {}).get(name)
            source_hash = document.get("sha256")
            if (
                not isinstance(content, str)
                or hashlib.sha256(content.encode()).hexdigest() != source_hash
            ):
                raise ExportError("Historical ETHOS snapshot failed integrity validation")
            # Never use source-controlled path components to form archive paths.
            doc_identity = digest([document.get("document_id"), document.get("version")])[:24]
            target = f"ethos/{doc_identity}/{source_hash}/ETHOS.md"
            clean = redactor.clean(content, target)
            files[target] = clean.encode()
            documents.append(
                redactor.clean(
                    {
                        "run_id": run["id"],
                        "path": target,
                        "source_sha256": source_hash,
                        "document_id": document.get("document_id"),
                        "version": document.get("version"),
                        "transformed": clean != content,
                        "mapping": mapping,
                        "applicable_records": applicable,
                        "requirements": document.get("requirements", []),
                        "effective_from": document.get("effective_from"),
                        "effective_until": document.get("effective_until"),
                    },
                    "ethos-mapping",
                )
            )
    validation, captures = (
        {
            "traces": [],
            "audit_schema": "not_applicable",
            "task_proof": "not_run",
            "agent_performance": "unmeasured",
        },
        [],
    )
    for finding in frozen["findings"]:
        refs = {(r["sequence"], r["content_hash"]) for r in finding["capture_records"]}
        events = [e for e in frozen["records"] if (e["sequence"], e["content_hash"]) in refs]
        missing, trace_name = [], None
        required_contract_refs = {
            (r.get("sequence"), r.get("content_hash"))
            for r in finding.get("evidence", [])
            if r.get("chain_id") == frozen["chain_id"]
        }
        finding_contract = finding.get("ethos", {})
        applicable_documents = documents
        if isinstance(finding_contract, dict) and finding_contract.get("sha256"):
            applicable_documents = [
                doc
                for doc in documents
                if doc["source_sha256"] == finding_contract["sha256"]
                and doc["document_id"] == finding_contract.get("document_id")
                and doc["version"] == finding_contract.get("version")
            ]
        available_contract_refs = {
            (r["sequence"], r["content_hash"])
            for doc in applicable_documents
            for r in doc["applicable_records"]
        }
        if not required_contract_refs or not required_contract_refs <= available_contract_refs:
            missing.append(
                "Applicable intended-behavior contract/ETHOS and mapping "
                "must be supplied externally"
            )
        if not events:
            missing.append("Supporting source records are unavailable")
        if subject == "council-decision":
            review = [e for e in events if e.get("entry_type") == "intent_review_recorded"]
            if not review or any(
                not isinstance(e.get("payload", {}).get("intent"), dict)
                or not isinstance(e["payload"].get("final"), dict)
                or not e["payload"]["final"].get("decision")
                for e in review
            ):
                missing.append("Explicit council request and decision are required")
            route = "repository-first-eval"
            status = "additional_context_required" if missing else "evidence_available"
        else:
            trace, reason = _atif(events, frozen["chain_id"])
            route = "atif-trace-environment" if trace else "manual-review"
            candidate_path = f"traces/{digest(trace)[:24]}.atif.json" if trace else None
            if trace and len(validation["traces"]) >= 4 and candidate_path not in files:
                trace, reason = (
                    None,
                    "Capture limit of four validated traces reached; export separately",
                )
            if trace:
                missing.append("Client agent version was not recorded; supply it externally")
                trace_name = candidate_path
                clean_trace = redactor.clean(trace, trace_name)
                if trace_name not in files:
                    result = _validate_atif(clean_trace)
                    files[trace_name] = encoded(clean_trace)
                    validation["traces"].append({"path": trace_name, **result})
                status = "additional_context_required" if missing else "evidence_available"
            else:
                missing.append(reason)
                status = "unsupported_for_trace_derivation"
        if any(o.get("finding_id") in {None, finding["id"]} for o in frozen["omissions"]):
            missing.append("See manifest omissions for unavailable or bounded historical evidence")
        if missing and status == "evidence_available":
            status = "additional_context_required"
        captures.append(
            {
                "finding_id": finding["id"],
                "subject": subject,
                "route": route,
                "status": status,
                "trace": trace_name,
                "missing_inputs": missing,
                "external_prerequisites": [
                    "Agent/council repository and runtime configuration",
                    "Initial environment state and tool definitions when not recorded",
                    "Harbor/Docker and provider access for actual agent trials",
                ],
            }
        )
    # Any content loss requires semantic review across all candidates sharing this capture.
    if redactor.changes:
        for capture in captures:
            if capture["status"] == "evidence_available":
                capture["status"] = "additional_context_required"
            capture["missing_inputs"].append("Review semantic effect of redactions/truncation")
    files["completeness.json"] = encoded(redactor.clean(captures))
    files["redactions.json"] = encoded(
        {
            "profile": PROFILE,
            "changes": redactor.changes,
            "unknown_payload_fields": "omitted by allowlist",
            "signature_status": (
                "original signatures omitted; transformed content is not "
                "independently signature-verifiable"
            ),
        }
    )
    files["validation.json"] = encoded(validation)
    manifest = {
        "schema": SCHEMA,
        "created_at": datetime.now(UTC).isoformat(),
        "subject": subject,
        **safe_metadata,
        "source_capture_sha256": digest(frozen),
        "profile": PROFILE,
        "conversion": "nomos-explicit-client-atif-v1",
        "eval_author_commit": AUTHOR_COMMIT,
        "compatibility": {
            "atif_version": "ATIF-v1.0",
            "harbor_pilot_version": "0.23.0",
            "council_route": "repository-first-eval",
        },
        "ethos": documents,
        "limits": {"findings": MAX_FINDINGS, "records": MAX_RECORDS, "source_bytes": MAX_BYTES},
        "file_inventory": {
            name: {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
            for name, content in files.items()
        },
        "inventory_scope": "All archive members except manifest.json (self-reference excluded)",
    }
    # Capture ID identifies exact serialized content, independent of later ledger checkpoints.
    capture_id = digest(manifest)
    manifest["capture_id"] = capture_id
    files["manifest.json"] = encoded(manifest)
    if sum(map(len, files.values())) > MAX_BYTES:
        raise ExportError("Export exceeds 8 MiB; select fewer findings")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            archive.writestr(name, content)
    preview = {
        "capture_id": capture_id,
        "subject": subject,
        "selected_finding_ids": manifest["selected_finding_ids"],
        "completeness": json.loads(files["completeness.json"]),
        "omissions": manifest["omissions"],
        "redaction_count": len(redactor.changes),
        "files": sorted(files),
        "bytes": len(output.getvalue()),
        "expires_in_seconds": TTL_SECONDS,
    }
    return output.getvalue(), preview


class CaptureCache:
    """Process-private snapshots, bounded to four exports for ten minutes; never persisted."""

    def __init__(self):
        self.lock = threading.Lock()
        self.items: dict[str, tuple[float, bytes, dict]] = {}

    def _expire(self):
        now = time.monotonic()
        self.items = {k: v for k, v in self.items.items() if now - v[0] < TTL_SECONDS}

    def prepare(self, store: Store, ids: list[str], subject: str, secrets: list[str]) -> dict:
        with self.lock:
            self._expire()
            if len(self.items) >= 4:
                self.items.pop(next(iter(self.items)))
            bundle, preview = build_capture(resolve_selection(store, ids), subject, secrets)
            self.items[preview["capture_id"]] = (time.monotonic(), bundle, preview)
            return preview

    def download(self, capture_id: str) -> bytes:
        with self.lock:
            self._expire()
            if capture_id not in self.items:
                raise ExportError("Capture expired or unavailable; preview the selection again")
            return self.items[capture_id][1]
