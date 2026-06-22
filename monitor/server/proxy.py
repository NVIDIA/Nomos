# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


"""Streaming MCP proxy with timing instrumentation.

`forward_mcp` is a thin reverse-proxy primitive. It sends the incoming
request body to the upstream council, streams the response back, and
records a metrics entry for each request — regardless of whether the
upstream succeeded.

Failures to record metrics MUST NOT break the proxy path. Anything that
looks like instrumentation is wrapped so a metrics exception cannot
escape into the request handler.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shlex
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx
from starlette.responses import StreamingResponse

from monitor.server.metrics import COUNCIL_TOOLS, DecisionSummary, MetricsRegistry, RequestSummary

logger = logging.getLogger(__name__)

# Standard hop-by-hop headers per RFC 7230 §6.1 plus content-length, which
# httpx sets correctly for the proxied body. Re-forwarding them creates
# duplicate or contradictory framing.
HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)

# Header agents may set to attribute their origin in the dashboard.
# Default when absent: "direct".
SOURCE_HEADER = "X-Council-Source"
DEFAULT_SOURCE = "direct"
MAX_RESPONSE_CAPTURE_BYTES = 256 * 1024
DECISION_TOOLS = COUNCIL_TOOLS


def _filter_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP_HEADERS}


def _extract_source(headers: Mapping[str, str]) -> str:
    """Return the X-Council-Source header value (case-insensitive) or default."""
    target = SOURCE_HEADER.lower()
    for k, v in headers.items():
        if k.lower() == target:
            stripped = v.strip()
            return stripped if stripped else DEFAULT_SOURCE
    return DEFAULT_SOURCE


def extract_method_and_tool(body: bytes) -> tuple[str, str]:
    """Parse a JSON-RPC body for method and (when tools/call) the tool name.

    Best-effort. Returns ('', '') for unparseable, non-object, or batch bodies
    — batches mix multiple methods and don't have a single attributable tool.
    """
    if not body:
        return ("", "")
    try:
        parsed = json.loads(body)
    except (ValueError, json.JSONDecodeError):
        return ("", "")
    if not isinstance(parsed, dict):
        return ("", "")
    method = parsed.get("method", "")
    if not isinstance(method, str):
        return ("", "")
    if method != "tools/call":
        return (method, "")
    params = parsed.get("params")
    tool = ""
    if isinstance(params, dict):
        name = params.get("name", "")
        if isinstance(name, str):
            tool = name
    return (method, tool)


def extract_request_summary(body: bytes) -> RequestSummary | None:
    """Extract sanitized request facts from a tools/call body."""
    try:
        parsed = json.loads(body)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    params = parsed.get("params")
    if not isinstance(params, dict):
        return None
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        return None

    command = _str(arguments.get("command"))
    return RequestSummary(
        agent_id=_label(arguments.get("agent_id")),
        command_hash=_sha256(command) if command else "",
        command_executable=_command_executable(command),
        packaged_file_count=_list_count(arguments.get("packaged_files")),
        evidence_count=_list_count(arguments.get("evidence")),
        env_key_count=_mapping_count(arguments.get("environment")),
    )


def extract_decision_summary(
    *,
    tool: str,
    request_summary: RequestSummary | None,
    response_body: bytes,
) -> DecisionSummary | None:
    """Parse a sanitized council decision summary from an MCP JSON-RPC response."""
    if tool not in DECISION_TOOLS or not response_body:
        return None

    payload = _extract_tool_payload(response_body)
    if payload is None:
        return None

    status = _str(payload.get("status"))
    state = _str(payload.get("state"))
    success = payload.get("success")
    error = _str(payload.get("error"))
    missing_permissions = tuple(
        p for p in (_str(v) for v in _as_list(payload.get("missing_permissions"))) if p
    )
    guidance_count = len(_as_list(payload.get("guidance")))

    if _is_propose_tool(tool):
        outcome = _propose_outcome(success=success, state=state, error=error)
    else:
        outcome = status or ("error" if error else "unknown")

    chairman = payload.get("chairman")
    chairman_obj = chairman if isinstance(chairman, dict) else {}
    code_review = _code_review_state(payload.get("code_review"))
    votes = _collect_votes(payload)

    return DecisionSummary(
        outcome=outcome,
        error_class=_classify_error(error, missing_permissions, outcome),
        risk_level=_str(chairman_obj.get("risk_level")),
        threshold=_str(chairman_obj.get("threshold")),
        approval_votes=votes["approve"],
        denial_votes=votes["deny"],
        abstain_votes=votes["abstain"],
        debate_rounds=_int(payload.get("debate_rounds")),
        changed_votes=votes["changed"],
        code_review=code_review,
        missing_permissions=missing_permissions,
        guidance_count=guidance_count,
        writ_issued=bool(payload.get("writ") or payload.get("writ_id")),
        decision_id=_str(payload.get("decision_id")),
        writ_id=_str(payload.get("writ_id")),
        request=request_summary,
    )


async def forward_mcp(
    *,
    client: httpx.AsyncClient,
    upstream_url: str,
    method: str,
    request_headers: Mapping[str, str],
    request_body: bytes,
    registry: MetricsRegistry,
) -> StreamingResponse:
    """Forward one HTTP request to the upstream MCP server, streaming the response.

    The metrics path is wrapped so an exception inside metrics code never
    breaks the proxy itself. The proxy path's own failure modes (connect
    error, read error, HTTP error response) all go through `complete` with
    ok=False and an appropriate status_code.
    """
    rpc_method, rpc_tool = extract_method_and_tool(request_body)
    request_summary = extract_request_summary(request_body)
    source = _extract_source(request_headers)

    handle = None
    if rpc_method:
        try:
            handle = await registry.start(
                method=rpc_method,
                tool=rpc_tool,
                source=source,
                request=request_summary,
            )
        except Exception:
            logger.exception("metrics.start failed; proxy will continue uninstrumented")

    upstream_headers = _filter_headers(request_headers)

    try:
        upstream_request = client.build_request(
            method=method,
            url=upstream_url,
            headers=upstream_headers,
            content=request_body,
        )
        upstream_response = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        logger.warning("upstream connect failed: %s", exc)
        if handle is not None:
            try:
                await registry.complete(handle, status_code=502, ok=False)
            except Exception:  # pragma: no cover - defensive
                logger.exception("metrics.complete failed")
        return StreamingResponse(
            iter([json.dumps({"error": "upstream unavailable", "detail": str(exc)}).encode()]),
            status_code=502,
            media_type="application/json",
        )

    response_headers = _filter_headers(upstream_response.headers)
    status_code = upstream_response.status_code
    ok = status_code < 500

    async def body_iter() -> AsyncIterator[bytes]:
        captured = bytearray()
        truncated = False
        try:
            async for chunk in upstream_response.aiter_raw():
                if not truncated and chunk:
                    remaining = MAX_RESPONSE_CAPTURE_BYTES - len(captured)
                    if remaining > 0:
                        captured.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        truncated = True
                yield chunk
        except httpx.HTTPError as exc:
            logger.warning("upstream stream error: %s", exc)
        finally:
            await upstream_response.aclose()
            if handle is not None:
                try:
                    decision = None
                    if not truncated:
                        decision = extract_decision_summary(
                            tool=rpc_tool,
                            request_summary=request_summary,
                            response_body=bytes(captured),
                        )
                    await registry.complete(
                        handle,
                        status_code=status_code,
                        ok=ok,
                        decision=decision,
                    )
                except Exception:  # pragma: no cover - defensive
                    logger.exception("metrics.complete failed")

    return StreamingResponse(
        body_iter(),
        status_code=status_code,
        headers=response_headers,
    )


def _extract_tool_payload(response_body: bytes) -> dict[str, Any] | None:
    parsed = _json_loads(response_body)
    if isinstance(parsed, dict):
        payload = _extract_tool_payload_from_json(parsed)
        if payload is not None:
            return payload

    for event in _iter_sse_json_events(response_body):
        payload = _extract_tool_payload_from_json(event)
        if payload is not None:
            return payload

    return None


def _is_propose_tool(tool: str) -> bool:
    return tool in {
        "council_propose_intent",
        "council_v1_propose_intent",
        "council_v2_propose_intent",
    }


def _extract_tool_payload_from_json(parsed: dict[str, Any]) -> dict[str, Any] | None:
    """Extract a tool payload from a decoded JSON-RPC object or raw payload."""
    # NAT/MCP streamable HTTP response shape.
    result = parsed.get("result")
    if isinstance(result, dict):
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        content = result.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                text = block.get("text")
                if not isinstance(text, str) or not text:
                    continue
                payload = _json_loads(text.encode("utf-8"))
                if isinstance(payload, dict):
                    return payload

    # Useful for unit tests and debug endpoints that return the tool payload
    # directly instead of wrapping it in JSON-RPC content.
    if any(k in parsed for k in ("status", "state", "success", "council_members")):
        return parsed
    return None


def _iter_sse_json_events(response_body: bytes) -> list[dict[str, Any]]:
    """Decode JSON payloads from text/event-stream frames.

    MCP streamable HTTP responses may arrive as SSE, where the JSON-RPC
    response is carried in one or more `data:` lines. The proxy captures the
    already-streamed bytes for metrics only, so this parser must be best-effort
    and must never affect the client response path.
    """
    try:
        text = response_body.decode("utf-8")
    except UnicodeDecodeError:
        text = response_body.decode("utf-8", errors="replace")

    events: list[dict[str, Any]] = []
    data_lines: list[str] = []

    def flush() -> None:
        if not data_lines:
            return
        data = "\n".join(data_lines).strip()
        data_lines.clear()
        if not data or data == "[DONE]":
            return
        parsed = _json_loads(data.encode("utf-8"))
        if isinstance(parsed, dict):
            events.append(parsed)

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if line == "":
            flush()
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))

    flush()
    return events


def _json_loads(data: bytes) -> Any:
    try:
        return json.loads(data)
    except (ValueError, json.JSONDecodeError):
        return None


def _collect_votes(payload: dict[str, Any]) -> dict[str, int]:
    votes = {"approve": 0, "deny": 0, "abstain": 0, "changed": 0}
    for member in _as_list(payload.get("council_members")):
        if not isinstance(member, dict):
            continue
        decision = _str(member.get("decision")).lower()
        if decision == "approve":
            votes["approve"] += 1
        elif decision == "deny":
            votes["deny"] += 1
        else:
            votes["abstain"] += 1
        if bool(member.get("changed_vote")):
            votes["changed"] += 1
    return votes


def _code_review_state(value: Any) -> str:
    if value in (None, "", False):
        return ""
    if isinstance(value, dict):
        decision = _str(value.get("decision")).lower()
        if decision:
            return decision
        status = _str(value.get("status")).lower()
        if status:
            return status
        return "present"
    return "present"


def _classify_error(error: str, missing_permissions: tuple[str, ...], outcome: str) -> str:
    if missing_permissions:
        return "permission"
    if not error:
        return ""
    lower = error.lower()
    if "auth" in lower or "token" in lower:
        return "auth"
    if "opa" in lower or "policy" in lower:
        return "opa"
    if "permission" in lower or "missing" in lower:
        return "permission"
    if "valid" in lower or "required" in lower or "invalid" in lower:
        return "validation"
    if outcome == "denied":
        return "council"
    return "upstream"


def _propose_outcome(*, success: Any, state: str, error: str) -> str:
    if error:
        return "error"
    if state:
        return state
    if success is True:
        return "accepted"
    if success is False:
        return "denied"
    return "unknown"


def _command_executable(command: str) -> str:
    if not command:
        return ""
    try:
        parts = shlex.split(command, posix=True)
    except ValueError:
        parts = command.split()
    if not parts:
        return ""
    return parts[0].rsplit("/", 1)[-1]


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _label(value: Any) -> str:
    return _str(value)[:96]


def _int(value: Any) -> int:
    return value if isinstance(value, int) else 0


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _list_count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def _mapping_count(value: Any) -> int:
    return len(value) if isinstance(value, dict) else 0
