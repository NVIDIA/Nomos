#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Replay a small fictional context-format matrix against Jev, or prepare it offline.

This measures model decisions, not Hermes execution or OpenShell client latency.
Only top-level supplementary conversation_context changes between formats. Exact
request duplicates share one actual assessment; cells retain that shared ID so
reports cannot mistake repeated labels for independent latency measurements.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import random
import subprocess
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
FORMATS = ("http_only", "full_json", "compact_json", "prose")
ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-1.14.0"


def encoded(value: Any) -> bytes:
    """Serialize actual request bytes deterministically, without label or cache salts."""
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode()


def digest(value: Any) -> str:
    """Identify exact serialized study inputs for deduplication and provenance."""
    return hashlib.sha256(encoded(value)).hexdigest()


def normalized_context(source: dict) -> dict:
    """Select evidence and express relationships directly, removing opaque bookkeeping.

    This intentionally changes selection as well as representation versus the
    full JSON envelope. Compact JSON and prose share this exact fact structure;
    only that pair supports a comparatively clean syntax-format comparison.
    """
    retained = source.get("retained_context", {})
    result: dict[str, Any] = {
        "evidence_scope": "Supplementary reported history, not additional outgoing HTTP content.",
        "retained_user_request": retained.get("latest_user_request", "User request unavailable."),
        "model_requests": [],
        "limitations": [
            "Context is workload-reported and not independently verified.",
            "Publication may lose events; this is not proof that all activity was captured.",
            "No exact tool call is attributed to the HTTP request under review.",
        ],
    }
    limits = result["limitations"]
    if retained.get("user_request_truncated") is not False:
        limits.append("The retained user request was shortened or its completeness is unknown.")
    omitted = source.get("omitted_returned_records", 0)
    returned = source.get("returned_tool_records", len(source.get("recent_tool_records", [])))
    if omitted:
        limits.append(f"{omitted} of {returned} returned tool events were omitted from this input.")
    if source.get("source_tool_records_truncated"):
        limits.append(
            "Additional live tool events existed beyond the context service's return limit."
        )
    groups: OrderedDict[str, dict] = OrderedDict()
    calls: dict[tuple[str, str], dict] = {}
    for index, record in enumerate(source.get("recent_tool_records", [])):
        parent = record["snapshot_id"]
        if parent not in groups:
            groups[parent] = {
                "relationship": "current retained model request"
                if parent == source.get("snapshot_ref")
                else "earlier model request; its retained user text is not supplied",
                "tool_calls": [],
            }
        call_key = (parent, record.get("tool_call_id") or f"unattributed-event-{index}")
        if call_key not in calls:
            calls[call_key] = {"tool": record["tool_name"], "events_newest_first": []}
            groups[parent]["tool_calls"].append(calls[call_key])
        event: dict[str, Any] = {
            "recency_rank": index + 1,
            "reported_execution_state": record["state"],
        }
        if "arguments" in record:
            event["arguments"] = copy.deepcopy(record["arguments"])
        if record.get("arguments_incomplete") is not False:
            event["argument_limitations"] = (
                "Some argument content was omitted or changed, or completeness is unknown."
            )
        review = record.get("council_review")
        if review is not None:
            event["reported_council_decision"] = review["status"]
            event["reported_council_guidance"] = copy.deepcopy(review.get("guidance", []))
            if review.get("guidance_truncated"):
                event["guidance_limitations"] = "Council guidance was shortened or omitted."
        else:
            event["reported_council_decision"] = "not supplied"
        calls[call_key]["events_newest_first"].append(event)
    result["model_requests"] = list(groups.values())
    return result


def prose_context(evidence: dict) -> str:
    """Render every normalized evidence fact in readable prose with quoted data.

    Plain language is not a trusted instruction channel. Values stay quoted and
    retain their exact contents, including malicious instructions in fixtures.
    """

    def quote(value: Any) -> str:
        """Keep evidence values quoted rather than treating their text as instructions."""
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    lines = [
        "Evidence scope: " + quote(evidence["evidence_scope"]),
        "Retained user request: " + quote(evidence["retained_user_request"]),
    ]
    for number, request in enumerate(evidence["model_requests"], 1):
        lines.append(f"Model request group {number}: " + quote(request["relationship"]))
        for call_number, call in enumerate(request["tool_calls"], 1):
            lines.append(
                f"Tool call {call_number} in that group is named "
                + quote(call["tool"])
                + ". Its reported events follow newest first."
            )
            for event_number, event in enumerate(call["events_newest_first"], 1):
                lines.append(
                    "Relative event recency rank across all groups (1 is newest): "
                    + quote(event["recency_rank"])
                )
                lines.append(
                    f"Event {event_number}: reported execution state is "
                    + quote(event["reported_execution_state"])
                    + "."
                )
                if "arguments" in event:
                    lines.append("Reported arguments: " + quote(event["arguments"]))
                if "argument_limitations" in event:
                    lines.append("Argument limitation: " + quote(event["argument_limitations"]))
                lines.append(
                    "Reported Council decision: " + quote(event["reported_council_decision"])
                )
                guidance_items = event.get("reported_council_guidance", [])
                if "reported_council_guidance" in event and not guidance_items:
                    lines.append("Reported Council guidance: [] (no guidance supplied).")
                for guidance in guidance_items:
                    lines.append("Reported Council guidance: " + quote(guidance))
                if "guidance_limitations" in event:
                    lines.append("Guidance limitation: " + quote(event["guidance_limitations"]))
    for limitation in evidence["limitations"]:
        lines.append("Evidence limitation: " + quote(limitation))
    return "\n".join(lines)


def make_state(case: dict, format_name: str, captured: dict | None = None) -> dict:
    """Keep HTTP evidence fixed and transform only the supplementary context field."""
    if format_name not in FORMATS:
        raise ValueError("unknown_format")
    if captured is not None:
        state = copy.deepcopy(captured)
    else:
        http = case["http"]
        state = {
            "observation_kind": "untrusted_http_request",
            "content_type": "application/json",
            **copy.deepcopy(http),
        }
        if case.get("context") is not None:
            state["conversation_context"] = copy.deepcopy(case["context"])
    context = state.pop("conversation_context", None)
    if format_name == "http_only" or context is None:
        return state
    if format_name == "full_json":
        state["conversation_context"] = context
    else:
        evidence = normalized_context(context)
        state["conversation_context"] = (
            evidence if format_name == "compact_json" else prose_context(evidence)
        )
    return state


def question_map(catalog: dict) -> dict:
    """Use the same noul instructions contract as the service's SystemOne provider."""
    return {
        rule["id"]: {"type": "noul", "instructions": rule["question"]} for rule in catalog["rules"]
    }


def build_cells(
    cases: list[dict],
    catalogs: dict,
    formats: list[str],
    model: str,
    seed: int,
    captured: dict | None = None,
) -> tuple[list[dict], dict]:
    """Rotate format order across cases and deduplicate exact complete requests."""
    # Reproducible experimental ordering; never used for credentials or IDs.
    rng = random.Random(seed)  # noqa: S311  # nosec B311
    ordered_cases = list(cases)
    rng.shuffle(ordered_cases)
    cells, requests = [], {}
    policies = list(catalogs)
    for case_index, case in enumerate(ordered_cases):
        policy_order = (
            policies[case_index % len(policies) :] + policies[: case_index % len(policies)]
        )
        for policy_index, policy in enumerate(policy_order):
            offset = (case_index + policy_index) % len(formats)
            order = formats[offset:] + formats[:offset]
            for format_name in order:
                request = {
                    "model": model,
                    "state": make_state(
                        case, format_name, None if captured is None else captured[case["id"]]
                    ),
                    "questions": question_map(catalogs[policy]),
                }
                sha = digest(request)
                requests.setdefault(sha, request)
                cells.append(
                    {
                        "case_id": case["id"],
                        "pair_id": case["pair_id"],
                        "format": format_name,
                        "policy": policy,
                        "expected": case["expected"],
                        "context_dependent": case.get("context_dependent", False),
                        "request_sha256": sha,
                    }
                )
    return cells, requests


def parse_scores(response: dict, model: str, question_ids: list[str]) -> dict[str, float]:
    """Reject alias drift and malformed/non-finite answers instead of making a decision."""
    if response.get("model") != model:
        raise ValueError("response_model_mismatch")
    scores = {}
    for question_id in question_ids:
        answer = response.get("answers", {}).get(question_id, {})
        score = answer.get("noul")
        if (
            answer.get("type") != "noul"
            or isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(score)
            or not 0 <= score <= 1
        ):
            raise ValueError("invalid_noul_answer")
        scores[question_id] = float(score)
    return scores


def assess(request: dict, key: str, timeout: float) -> tuple[dict, dict | str]:
    """Make one serial HTTPS assessment, without implicit redirects or retries."""

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    wire = encoded(request)
    call = urllib.request.Request(  # noqa: S310 - fixed HTTPS endpoint, not workload input.
        ENDPOINT,
        data=wire,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    started = time.perf_counter()
    result: dict[str, Any] = {
        "request_sha256": digest(request),
        "request_bytes": len(wire),
        "started_at": datetime.now(UTC).isoformat(),
    }
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(call, timeout=timeout) as response:
            raw = response.read(256 * 1024 + 1)
            result["http_status"] = response.status
        result["latency_ms"] = (time.perf_counter() - started) * 1000
        if len(raw) > 256 * 1024:
            raise ValueError("response_over_limit")
        body = json.loads(raw)
        result["scores"] = parse_scores(body, request["model"], list(request["questions"]))
        result["status"] = "ok"
        result["response_model"] = body["model"]
        result["usage"] = body.get("usage")
        return result, body
    except urllib.error.HTTPError as error:
        result.update(status="http_error", http_status=error.code)
    except (urllib.error.URLError, TimeoutError, OSError):
        result["status"] = "transport_error"
    except (ValueError, TypeError, KeyError):
        result["status"] = "invalid_response"
    result.setdefault("latency_ms", (time.perf_counter() - started) * 1000)
    # Never retain reflected remote error text or exception strings that could expose credentials.
    return result, "Response unavailable or invalid; no automatic retry."


def write_json(path: Path, value: Any) -> None:
    """Write readable artifacts without an authentication header or API credential."""
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n")


def main() -> None:
    """Prepare a reviewed matrix by default; live calls require an explicit switch."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", type=Path, default=ROOT / "cases.json")
    parser.add_argument("--questions", type=Path, default=ROOT / "questions.json")
    parser.add_argument("--captured-states", type=Path)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--seed", type=int, default=20261101)
    parser.add_argument("--timeout", type=float, default=8)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--case-id", action="append")
    parser.add_argument("--policy", action="append", choices=("baseline", "context_aware"))
    parser.add_argument("--format", action="append", choices=FORMATS)
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("timeout must be finite and positive")
    cases_doc = json.loads(args.cases.read_text())
    cases = [case for case in cases_doc["cases"] if not args.case_id or case["id"] in args.case_id]
    if not cases or (args.case_id and len(cases) != len(set(args.case_id))):
        parser.error("case selection is empty or contains unknown IDs")
    catalogs = json.loads(args.questions.read_text())["catalogs"]
    if args.policy:
        catalogs = {name: catalogs[name] for name in args.policy}
    for catalog in catalogs.values():
        for rule in catalog["rules"]:
            if len(rule["question"].encode()) > 1024 or rule["deny_when"] != "yes":
                parser.error("unsupported question catalog")
    captured = None
    if args.captured_states:
        captured = json.loads(args.captured_states.read_text())["states"]
        if not all(case["id"] in captured for case in cases):
            parser.error("captured states do not cover selected cases")
    key = os.environ.get("JEV_API_KEY", "")
    if args.execute and not key:
        parser.error("JEV_API_KEY must be set; do not pass credentials on the command line")
    if args.output.exists() and any(args.output.iterdir()):
        parser.error(
            "output directory must be new or empty; measured evidence is never overwritten"
        )
    args.output.mkdir(parents=True, exist_ok=True)
    cells, requests = build_cells(
        cases,
        catalogs,
        list(dict.fromkeys(args.format or FORMATS)),
        args.model,
        args.seed,
        captured,
    )
    for folder in ("requests", "responses"):
        (args.output / folder).mkdir()
    for sha, request in requests.items():
        write_json(args.output / "requests" / f"{sha}.json", request)
    try:
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
    except subprocess.CalledProcessError:
        revision = "unavailable"
    manifest = {
        "schema_version": 1,
        "study_kind": "exploratory model-only context-format replay",
        "execute": args.execute,
        "endpoint": ENDPOINT,
        "model": args.model,
        "source_commit": revision,
        "seed": args.seed,
        "case_count": len(cases),
        "matrix_cells": len(cells),
        "unique_model_requests": len(requests),
        "cases_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        "questions_sha256": hashlib.sha256(args.questions.read_bytes()).hexdigest(),
        "captured_states_sha256": None
        if not args.captured_states
        else hashlib.sha256(args.captured_states.read_bytes()).hexdigest(),
        "cache_policy": (
            "exact duplicates share one assessment; no nonce/salt; remote cache behavior unknown"
        ),
        "concurrency": 1,
        "timeout_seconds": args.timeout,
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    write_json(args.output / "manifest.json", manifest)
    write_json(args.output / "matrix.json", cells)
    measurements = {}
    with (args.output / "measurements.jsonl").open("x") as log:
        if args.execute:
            for index, (sha, request) in enumerate(requests.items(), 1):
                result, raw = assess(request, key, args.timeout)
                result["measurement_id"] = f"measurement-{index:03d}"
                measurements[sha] = result
                write_json(args.output / "responses" / f"{sha}.json", raw)
                log.write(json.dumps(result, allow_nan=False) + "\n")
                log.flush()
                print(f"{index}/{len(requests)} {result['status']}", flush=True)
    output = []
    for cell in cells:
        result = {**cell, "measurement": measurements.get(cell["request_sha256"])}
        measurement = result["measurement"]
        if measurement and measurement["status"] == "ok":
            rules = catalogs[cell["policy"]]["rules"]
            result["decision"] = (
                "deny"
                if any(measurement["scores"][r["id"]] >= r["threshold"] for r in rules)
                else "allow"
            )
            result["matches_expected"] = result["decision"] == cell["expected"]
        output.append(result)
    write_json(
        args.output / "results.json",
        {"manifest": manifest, "cells": output, "unique_measurements": list(measurements.values())},
    )
    print(
        json.dumps(
            {
                "cases": len(cases),
                "cells": len(cells),
                "unique_requests": len(requests),
                "executed": args.execute,
            }
        )
    )


if __name__ == "__main__":
    main()
