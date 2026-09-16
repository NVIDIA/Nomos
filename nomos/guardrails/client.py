# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


"""Client and input shaping for the NeMo Guardrails prose screen.

The screen sends the intent's agent-authored prose channels to a guardrails
sidecar (``nemoguardrails server``) as a single labelled bundle and normalizes
the response into a blocked/clean verdict. ``command`` and ``packaged_files``
are never part of the bundle: they are code by design and are judged by OPA,
the code reviewer, and the council members.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from nomos.config.settings import GuardrailsConfig
from nomos.level_0.types import Intent


class GuardrailsEvaluationError(RuntimeError):
    """Raised when the guardrails sidecar cannot return a usable verdict."""


@dataclass(frozen=True)
class GuardrailsScreenDecision:
    """Normalized screening verdict.

    ``blocked`` is the sidecar's raw verdict; ``allow`` is the effective
    outcome after the configured mode is applied (shadow mode records a block
    but never denies).
    """

    allow: bool
    blocked: bool = False
    findings: list[str] = field(default_factory=list)
    guidance: list[str] = field(default_factory=list)
    mode: str = "enforce"
    channels: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def build_screen_text(intent: Intent, channels: Iterable[str]) -> tuple[str, list[str]]:
    """Bundle the selected prose channels into one labelled screening message.

    Returns ``(text, channels_included)`` where ``channels_included`` lists the
    channels that actually carried content. Channel labels let rail findings be
    traced back to the offending field by a human reader.
    """
    sections: list[tuple[str, str]] = []
    for channel in channels:
        if channel == "description":
            value = intent.description or ""
        elif channel == "justification":
            value = intent.justification or ""
        elif channel == "context":
            value = intent.context or ""
        elif channel == "blast_radius":
            value = intent.blast_radius or ""
        elif channel == "evidence":
            value = "\n\n".join(item for item in intent.evidence if item.strip())
        elif channel == "preceding_commands":
            value = "\n".join(item for item in intent.preceding_commands if item.strip())
        elif channel == "rollback_steps":
            steps = intent.rollback_plan.steps if intent.rollback_plan is not None else []
            value = "\n".join(step for step in steps if step.strip())
        else:  # pragma: no cover - config validation rejects unknown channels
            continue
        if value.strip():
            sections.append((channel, value))

    text = "\n\n".join(f"[channel: {name}]\n{value}" for name, value in sections)
    return text, [name for name, _ in sections]


class GuardrailsHttpClient:
    """Small async wrapper around the guardrails server's chat-completions API."""

    def __init__(self, config: GuardrailsConfig):
        self._url = config.url
        self._config_id = config.config_id
        self._timeout_seconds = config.timeout_ms / 1000

    async def evaluate(self, text: str) -> GuardrailsScreenDecision:
        """Screen one prose bundle, returning the raw (mode-less) verdict."""
        payload = await asyncio.to_thread(self._post, text)
        return parse_screen_response(payload)

    def _post(self, text: str) -> dict[str, Any]:
        # Contract pinned against nemoguardrails 0.24.0: `model` is required by
        # the OpenAI schema but unused by a models:[] screening profile;
        # generation options nest under `guardrails` and restrict the run to
        # input rails only, so a clean message echoes back without any LLM call.
        request_body = json.dumps(
            {
                "model": "screen",
                "messages": [{"role": "user", "content": text}],
                "guardrails": {
                    "config_id": self._config_id,
                    "options": {
                        "rails": ["input"],
                        "log": {"activated_rails": True},
                    },
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 - URL is operator-configured sidecar.
            f"{self._url}/v1/chat/completions",
            data=request_body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:  # noqa: S310
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise GuardrailsEvaluationError(
                f"Guardrails server returned HTTP {exc.code}: {details}"
            ) from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            raise GuardrailsEvaluationError(f"Guardrails request failed: {exc}") from exc

        try:
            decoded = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise GuardrailsEvaluationError(
                f"Guardrails server returned invalid JSON: {exc}"
            ) from exc
        if not isinstance(decoded, dict):
            raise GuardrailsEvaluationError("Guardrails response must be a JSON object")
        return decoded


def parse_screen_response(payload: Mapping[str, Any]) -> GuardrailsScreenDecision:
    """Normalize a guardrails server response into a raw screening verdict.

    Preference order (contract pinned against nemoguardrails 0.24.0):
    1. The ``guardrails.log.activated_rails`` trace (requested via generation
       options): a rail entry with ``stop: true`` names the finding, and its
       executed actions carry the specific ``detections`` (e.g. sql_injection).
    2. The response message content: the bundled rails profiles configure their
       refusals to carry the ``NOMOS_SCREEN_BLOCKED`` sentinel, so a blocked
       exchange is detectable even when the log section is absent.

    A *clean* verdict requires positive evidence that the input rails actually
    ran, i.e. a non-empty ``activated_rails`` trace. Absence of a stop signal is
    not evidence of a clean message: a rail whose sources fail to compile logs a
    warning and returns "no detection" from inside the rail, so the server still
    answers 200 with ``stop: false``. Treating that as clean would let a sidecar
    whose rules silently failed to load pass every intent while its health probe
    stayed green, and nothing in the ledger would distinguish "screened and
    clean" from "never screened". Without that evidence this raises, and the
    caller's fail-closed path denies — matching ``parse_opa_decision``, which
    also denies on a semantically empty 200.

    A *blocked* verdict needs no such evidence: denying is the safe direction.
    """
    findings: list[str] = []
    blocked = False

    rails_ran = False
    guardrails_section = payload.get("guardrails")
    log_section = guardrails_section.get("log") if isinstance(guardrails_section, Mapping) else None
    if isinstance(log_section, Mapping):
        activated = log_section.get("activated_rails")
        if isinstance(activated, list):
            rails_ran = bool(activated)
            for rail in activated:
                if not isinstance(rail, Mapping):
                    continue
                name = str(rail.get("name", "")).strip() or "unnamed rail"
                decisions = rail.get("decisions")
                stopped = bool(rail.get("stop")) or (
                    isinstance(decisions, list) and "stop" in decisions
                )
                if stopped:
                    blocked = True
                    detections = _rail_detections(rail)
                    detail = f" ({', '.join(detections)})" if detections else ""
                    findings.append(f"input rail blocked the message: {name}{detail}")

    content = _response_content(payload)
    if not blocked and "NOMOS_SCREEN_BLOCKED" in content:
        blocked = True
        # Deliberately not the response body: that text is echoed back from the
        # agent's own prose, and findings are recorded verbatim into the
        # hash-chained review record. Letting the subject of the screen author
        # the screen's verdict text would be an accountability defect, and the
        # bloat rail permits up to max_chars of it. The content is still carried
        # — truncated — in metadata below.
        findings.append("input rail blocked the message: refusal sentinel in screen response")

    if not blocked and not rails_ran:
        raise GuardrailsEvaluationError(
            "guardrails response carried no activated_rails; the screen cannot be "
            "confirmed to have run, so a clean verdict is not trustworthy"
        )

    return GuardrailsScreenDecision(
        allow=not blocked,
        blocked=blocked,
        findings=findings,
        metadata={"content": content[:500]} if blocked else {},
    )


def _rail_detections(rail: Mapping[str, Any]) -> list[str]:
    """Collect the named detections from a rail's executed actions."""
    detections: list[str] = []
    actions = rail.get("executed_actions")
    if not isinstance(actions, list):
        return detections
    for action in actions:
        if not isinstance(action, Mapping):
            continue
        return_value = action.get("return_value")
        if not isinstance(return_value, Mapping):
            continue
        metadata = return_value.get("metadata")
        if not isinstance(metadata, Mapping):
            continue
        found = metadata.get("detections")
        if isinstance(found, list):
            detections.extend(str(item) for item in found if str(item).strip())
    return detections


def _response_content(payload: Mapping[str, Any]) -> str:
    """Extract the assistant message content from either response shape."""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
        if isinstance(message, Mapping):
            content = message.get("content")
            if isinstance(content, str):
                return content
    messages = payload.get("messages")
    if isinstance(messages, list) and messages:
        last = messages[-1]
        if isinstance(last, Mapping):
            content = last.get("content")
            if isinstance(content, str):
                return content
    return ""
