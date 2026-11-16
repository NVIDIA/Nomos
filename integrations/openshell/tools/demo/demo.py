#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Present the OpenShell and Nomos middleware demo and verify decision evidence.

Every payload is fabricated. Requests originate from curl inside the dedicated
OpenShell sandbox. Only the host-side Nomos service has the Jev credential.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class DemoSettings:
    """Operator-selected local deployment addresses and evidence paths."""

    gateway: str
    sandbox: str
    destination_url: str
    sandbox_destination_url: str
    middleware_log: Path
    output_dir: Path


class DemoError(RuntimeError):
    """A deployment, response or evidence mismatch that must not produce PASS."""


def require(condition: bool, message: str) -> None:
    """Keep verification active even when Python is run with optimization."""
    if not condition:
        raise DemoError(message)


def parse_curl_response(name: str, result: subprocess.CompletedProcess[bytes]) -> tuple[int, dict]:
    """Validate sandbox curl output and report failures with the demo step name.

    Nonzero exits include bounded captured stderr for troubleshooting. A zero
    exit still requires an HTTP status, a header/body separator and a JSON object
    before the response can be compared with independent forwarding evidence.
    """
    require(
        result.returncode == 0,
        f"{name}: curl command exited {result.returncode}: "
        f"{result.stderr.decode(errors='replace')[:500]}",
    )
    headers, separator, body = result.stdout.partition(b"\r\n\r\n")
    status_line = headers.partition(b"\r\n")[0]
    match = re.fullmatch(rb"HTTP/(?:1\.[01]|2|3) ([1-5][0-9]{2})(?: [^\r\n]*)?", status_line)
    if not separator or match is None:
        raise DemoError(f"{name}: malformed HTTP response.")
    try:
        response = json.loads(body)
    except ValueError as error:
        raise DemoError(f"{name}: response body is not JSON.") from error
    require(isinstance(response, dict), f"{name}: response body is not a JSON object.")
    return int(match[1]), response


def load_settings(path: Path) -> DemoSettings:
    """Load only non-secret operator settings; resolve logs beside the config."""
    path = path.expanduser().resolve()
    try:
        config = yaml.safe_load(path.read_text())
    except (OSError, ValueError, yaml.YAMLError) as error:
        raise DemoError(
            f"Cannot read demo config {path}; prepare deployment.yaml as documented."
        ) from error
    required = {"gateway", "sandbox", "destination_url", "sandbox_destination_url"}
    allowed = required | {"middleware_log"}
    require(isinstance(config, dict), "Demo settings must be a YAML mapping.")
    require(
        required <= config.keys() and config.keys() <= allowed,
        "Demo settings require gateway, sandbox, destination_url and "
        "sandbox_destination_url; only middleware_log is optional. Never add credentials.",
    )
    require(
        all(isinstance(value, str) and value.strip() for value in config.values()),
        "Demo settings must contain nonempty strings.",
    )
    for field in ("destination_url", "sandbox_destination_url"):
        parsed = urlsplit(config[field])
        require(
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and parsed.path in {"", "/"},
            f"{field} must be an HTTP(S) base URL without credentials, path, query or fragment.",
        )
    log_path = Path(config.get("middleware_log", "middleware.log")).expanduser()
    if not log_path.is_absolute():
        log_path = path.parent / log_path
    return DemoSettings(
        gateway=config["gateway"],
        sandbox=config["sandbox"],
        destination_url=config["destination_url"],
        sandbox_destination_url=config["sandbox_destination_url"],
        middleware_log=log_path,
        output_dir=path.parent,
    )


CASES = {
    "public": {
        "title": "Public information is allowed and reaches the destination",
        "body": {
            "message": (
                "The community garden workshop starts Saturday at 10 AM. Everyone is welcome."
            )
        },
        "path": "/publish",
        "status": 200,
        "error": None,
        "outcome": "allow",
    },
    "credential": {
        "title": "A populated password is denied by Nomos",
        "body": {
            "message": "Use the following account password to sign in to the service.",
            # Fabricated, nonfunctional value used only in this synthetic demo.
            "password": "demo-only-Maple!79-Harbor",  # pragma: allowlist secret
        },
        "path": "/publish",
        "status": 403,
        "error": "middleware_denied",
        "outcome": "deny",
    },
    "confidential": {
        "title": "Confidential business content is denied by Nomos",
        "body": {
            "message": (
                "Internal confidential acquisition plan: FictionalCo will acquire "
                "ExampleCo for $42 million next Monday. The announcement is under embargo."
            )
        },
        "path": "/publish",
        "status": 403,
        "error": "middleware_denied",
        "outcome": "deny",
    },
    "network-policy": {
        "title": "OpenShell independently denies an unapproved route",
        "body": {"message": "The community workshop starts Saturday."},
        "path": "/admin",
        "status": 403,
        "error": "policy_denied",
        "outcome": None,
    },
}


def destination_records(settings: DemoSettings) -> dict:
    """Read capture evidence through the host-side inspection listener."""
    # load_settings permits only HTTP(S) URLs without embedded credentials.
    with urllib.request.urlopen(  # noqa: S310  # nosec B310
        settings.destination_url.rstrip("/") + "/requests", timeout=5
    ) as response:
        return json.load(response)


def log_events(settings: DemoSettings) -> list[dict]:
    """Read structured middleware events; no payload or API credential is logged."""
    # A pipe/tee writer may have written only part of the final JSONL record.
    return [
        json.loads(line)
        for line in settings.middleware_log.read_text().splitlines(keepends=True)
        if line.endswith("\n") and line.strip()
    ]


def collect_request_events(settings: DemoSettings, start: int, expect_review: bool) -> list[dict]:
    """Wait at most one second for log delivery, never retrying a network request."""
    deadline = time.monotonic() + 1.0
    while True:
        all_events = log_events(settings)
        require(len(all_events) >= start, "Middleware log was restarted during the step.")
        events = all_events[start:]
        terminal = any(
            e.get("fields", {}).get("message") in {"review_complete", "review_failed"}
            for e in events
        )
        if terminal or (not expect_review and events):
            return events
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return events  # The verifier reports missing/incomplete evidence.
        time.sleep(min(0.02, remaining))


def verify_evidence(
    name: str,
    payload: bytes,
    status: int,
    response: dict,
    before: dict,
    after: dict,
    events: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Verify decisions and actual arrivals; never mistake an assessment error for denial."""
    case = CASES[name]
    require(status == case["status"], f"{name}: expected HTTP {case['status']}, received {status}.")
    require(
        response.get("error") == case["error"],
        f"{name}: unexpected response error; expected {case['error']}.",
    )
    arrivals = after["received_count"] - before["received_count"]
    require(
        arrivals == int(case["outcome"] == "allow"),
        f"{name}: unexpected destination arrival count {arrivals}.",
    )
    reviews = [
        e["fields"] for e in events if e.get("fields", {}).get("message") == "review_complete"
    ]
    calls = [
        e["fields"] for e in events if e.get("fields", {}).get("message") == "model_call_finished"
    ]
    if case["outcome"] is None:
        # A policy-only rejection must not even start an assessment/model call.
        require(
            not events,
            f"{name}: middleware events appeared for a policy-only request; "
            "use a dedicated serial demo.",
        )
    else:
        require(
            len(reviews) == 1 and len(calls) == 1,
            f"{name}: expected one completed review and model call; inspect middleware.log.",
        )
        require(reviews[0]["outcome"] == case["outcome"], f"{name}: unexpected Nomos decision.")
        require(
            calls[0].get("model_outcome") == "complete"
            and calls[0].get("model_completion_observed") is True,
            f"{name}: the model did not complete successfully.",
        )
        validated = [
            e["fields"]
            for e in events
            if e.get("fields", {}).get("message") == "review_input_validated"
        ]
        require(
            len(validated) == 1
            and validated[0].get("body_sha256") == hashlib.sha256(payload).hexdigest(),
            f"{name}: logged input does not match the sent body.",
        )
        request_ids = {e.get("span", {}).get("request_id") for e in events}
        require(
            len(request_ids) == 1 and None not in request_ids,
            f"{name}: unrelated or uncorrelated log events appeared; use a dedicated serial demo.",
        )
    if arrivals:
        received = [r for r in after["requests"] if r["request_id"] > before["received_count"]]
        require(len(received) == 1, f"{name}: expected one retained destination record.")
        record = received[0]
        require(
            record["method"] == "POST" and record["path"] == case["path"],
            f"{name}: method/path changed in transit.",
        )
        require(
            base64.b64decode(record["body_base64"], validate=True) == payload,
            f"{name}: destination body differs from the original bytes.",
        )
        require(
            record["sha256"] == hashlib.sha256(payload).hexdigest(),
            f"{name}: destination body hash mismatch.",
        )
    return reviews, calls


def demonstrate(name: str, settings: DemoSettings) -> dict:
    """Send one sandbox request and fail if status, decision or forwarding differs."""
    case = CASES[name]
    payload = json.dumps(case["body"], separators=(",", ":"), ensure_ascii=False).encode()
    print("\n" + "=" * 72)
    print(case["title"])
    print(f"POST {settings.sandbox_destination_url.rstrip('/')}{case['path']}")
    print(json.dumps(case["body"], indent=2))
    before = destination_records(settings)
    start_event = len(log_events(settings))
    command = [
        "openshell",
        "--gateway",
        settings.gateway,
        "sandbox",
        "exec",
        "--name",
        settings.sandbox,
        "--no-tty",
        "--",
        "/usr/bin/curl",
        "--silent",
        "--show-error",
        "--include",
        "--max-time",
        "15",
        f"{settings.sandbox_destination_url.rstrip('/')}{case['path']}",
        "--header",
        "content-type: application/json",
        "--data-binary",
        "@-",
    ]
    started = time.monotonic()
    result = subprocess.run(command, input=payload, capture_output=True, timeout=25, check=False)
    elapsed = (time.monotonic() - started) * 1000
    status, response = parse_curl_response(name, result)
    after = destination_records(settings)
    events = collect_request_events(settings, start_event, case["outcome"] is not None)
    arrivals = after["received_count"] - before["received_count"]
    # Show unexpected responses too, so setup failures remain understandable.
    print(f"\nHTTP {status}")
    print(json.dumps(response, indent=2))
    reviews, model_calls = verify_evidence(name, payload, status, response, before, after, events)
    if reviews:
        review = reviews[0]
        print("\nJev scores (deny when either is >= 0.70):")
        print(json.dumps(json.loads(review["scores"]), indent=2))
        print(
            f"Nomos: {review['outcome']}; matched rules: {review['matched_rules']}; "
            f"evaluation: {review['total_ms']} ms"
        )
        print(f"Request ID: {review['request_id']}")
        print(
            f"Model-reported tokens: {model_calls[0].get('input_tokens')} input, "
            f"{model_calls[0].get('output_tokens')} output"
        )
    else:
        print("\nOpenShell policy denied the route; Nomos and Jev were not called.")
    print(
        f"Destination arrivals for this request: {arrivals}"
        + ("; method, path and exact body bytes verified." if arrivals else ".")
    )
    print("PASS")
    return {
        "case": name,
        "status": status,
        "response": response,
        "destination_arrivals": arrivals,
        "reviews": reviews,
        "model_calls": model_calls,
        "client_command_ms": elapsed,
        "body_sha256": hashlib.sha256(payload).hexdigest(),
        "passed": True,
    }


def main() -> None:
    """Run all four steps or a selected step, optionally pausing for narration."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=CASES, help="Run only one demonstration step.")
    parser.add_argument("--pause", action="store_true", help="Wait for Enter before each step.")
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "deployment.yaml",
        help="Local YAML demo settings; never put an API token in this file.",
    )
    args = parser.parse_args()
    settings = load_settings(args.config)
    # Only validated non-secret settings are included in saved demo evidence.
    config = {
        "gateway": settings.gateway,
        "sandbox": settings.sandbox,
        "destination_url": settings.destination_url,
        "sandbox_destination_url": settings.sandbox_destination_url,
        "middleware_log": str(settings.middleware_log),
    }
    if not settings.middleware_log.is_file():
        parser.error(
            f"Middleware log does not exist: {settings.middleware_log}; "
            "start the middleware with tee as shown in README.md."
        )
    print("OpenShell and Nomos middleware")
    print("Real Jev assessments; synthetic requests only.")
    results = []
    for name in [args.case] if args.case else CASES:
        if args.pause:
            input(f"\nPress Enter to run {name}...")
        results.append(demonstrate(name, settings))
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output = settings.output_dir / ("validation-" + stamp + ".json")
    output.write_text(
        json.dumps(
            {
                "timestamp": stamp,
                "deployment": config,
                "results": results,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"\n{len(results)}/{len(results)} demo steps passed. Evidence: {output.name}")
    print("Live model results can vary; these are smoke checks, not an accuracy benchmark.")


if __name__ == "__main__":
    try:
        main()
    except (DemoError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"Demo failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
