# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Deterministic Jev replay transport; one attempt per request, no retry or redirect."""

from __future__ import annotations

import hashlib
import json
import math
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ENDPOINT = "https://api.typesafe.ai/v1/systemone"


def encoded(value: Any) -> bytes:
    """Serialize actual request bytes deterministically, without label or cache salts."""
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode()


def digest(value: Any) -> str:
    """Identify exact serialized study inputs for deduplication and provenance."""
    return hashlib.sha256(encoded(value)).hexdigest()


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
    call = urllib.request.Request(
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
