# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Client and input shaping for the Jev pre-council stage (member zero).

A Jev backend takes a `state` plus typed `questions` and returns typed
probabilistic answers. This module loads the question catalog, builds the
(redacted) state from an intent, calls the backend, and normalizes the response
into an ``AttestationVector``.

Two backends are supported, and ``JevConfig.provider`` is the only switch
between them: ``openjev`` (NVIDIA-hosted) and ``typesafe`` (hosted Jev System
One). The adaptation rule is that the CATALOG always carries criteria and the
ADAPTER decides whether to send them: openjev accepts ``criteria`` on every
question type, hosted Jev rejects them on ``noul``, so ``build_request`` drops
noul criteria on the typesafe path. Nothing in the catalog is backend-specific.

The pure helpers (``load_catalog``, ``build_state``, ``build_request``,
``parse_response``) are network-free and unit-testable. Only ``evaluate`` /
``_post`` touch the network, mirroring ``nomos.opa.client``.

The hosted-Jev response shape was confirmed live on 2026-09-21 and 2026-10-26:
``{model, answers: {id: {type, noul | choice+confidence+probabilities |
score+confidence}}, usage}``. ``parse_response`` stays tolerant of envelope
variations; the router (``nomos.jev.router``) is strict about the answers it
acts on.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from nomos.config.settings import JevConfig, JevProvider
from nomos.level_0.types import Attestation, AttestationVector, Intent
from nomos.level_0.types.attestation import QuestionType

from .redact import redact

_VALID_TYPES = {t.value for t in QuestionType}
# Every question declares its tier. `triage` answers are the read-only fast-path
# inputs, `risk` is the chairman's risk estimate, and `attestation` answers ride
# onto the Intent for the council. One request carries every tier.
_VALID_TIERS = {"triage", "risk", "attestation"}
# `adverse`: high is the concerning reading. `safe`: high is the safe reading,
# allowed only on the single triage gate.
_VALID_POLARITIES = {"adverse", "safe"}

# How an attestation's confidence was obtained, recorded in ``Attestation.detail``
# so a threshold can never mistake the proxy for a calibrated number.
CONFIDENCE_REPORTED = "reported"
CONFIDENCE_CERTAINTY_PROXY = "certainty_proxy"
CONFIDENCE_UNAVAILABLE = "unavailable"


class JevEvaluationError(RuntimeError):
    """Raised when Jev cannot return a usable attestation."""


@dataclass(frozen=True)
class CatalogQuestion:
    """One catalog question."""

    id: str
    category: str
    qtype: QuestionType
    action: str = "informational"
    tier: str = "attestation"
    polarity: str = "adverse"
    instructions: str = ""
    # Jev requires `criteria` for score (ordered list) and choice (object). openjev
    # also accepts a {"true": ..., "false": ...} mapping on noul; hosted Jev does
    # not, and `build_request` drops it there. None when the catalog omits it.
    criteria: Any = None


@dataclass(frozen=True)
class AttestationCatalog:
    """A versioned set of attestation questions."""

    version: str
    questions: list[CatalogQuestion]

    @property
    def gate(self) -> CatalogQuestion:
        """The triage gate: the one question whose high reading is the safe one."""
        return next(q for q in self.questions if q.polarity == "safe")


def load_catalog(path: str | Path) -> AttestationCatalog:
    """Load and validate the attestation question catalog from YAML.

    The version key is accepted as either ``version`` (PLAN.md 13.4) or the
    older ``catalog_version``, so a catalog revision can land independently of
    this loader.
    """
    text = Path(path).read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise JevEvaluationError("Attestation catalog must be a YAML mapping")
    version = str(data.get("version") or data.get("catalog_version") or "").strip()
    if not version:
        raise JevEvaluationError("Attestation catalog is missing version")
    raw_questions = data.get("questions") or []
    if not isinstance(raw_questions, list) or not raw_questions:
        raise JevEvaluationError("Attestation catalog has no questions")

    questions: list[CatalogQuestion] = []
    seen: set[str] = set()
    for entry in raw_questions:
        if not isinstance(entry, dict):
            raise JevEvaluationError("Each catalog question must be a mapping")
        qid = str(entry.get("id", "")).strip()
        qtype = str(entry.get("type", "")).strip()
        if not qid:
            raise JevEvaluationError("Catalog question is missing an id")
        if qid in seen:
            raise JevEvaluationError(f"Duplicate catalog question id: {qid}")
        if qtype not in _VALID_TYPES:
            valid = sorted(_VALID_TYPES)
            raise JevEvaluationError(
                f"Question {qid!r} has invalid type {qtype!r}; expected one of {valid}"
            )
        tier = str(entry.get("tier", "attestation")).strip() or "attestation"
        if tier not in _VALID_TIERS:
            valid_tiers = sorted(_VALID_TIERS)
            raise JevEvaluationError(
                f"Question {qid!r} has invalid tier {tier!r}; expected one of {valid_tiers}"
            )
        polarity = str(entry.get("polarity", "adverse")).strip() or "adverse"
        if polarity not in _VALID_POLARITIES:
            raise JevEvaluationError(
                f"Question {qid!r} has invalid polarity {polarity!r}; "
                f"expected one of {sorted(_VALID_POLARITIES)}"
            )
        seen.add(qid)
        questions.append(
            CatalogQuestion(
                id=qid,
                category=str(entry.get("category", "uncategorized")).strip() or "uncategorized",
                qtype=QuestionType(qtype),
                action=str(entry.get("action", "informational")).strip() or "informational",
                tier=tier,
                polarity=polarity,
                instructions=str(entry.get("instructions", "")).strip(),
                criteria=entry.get("criteria"),
            )
        )
    gates = [q for q in questions if q.polarity == "safe"]
    if len(gates) != 1 or gates[0].tier != "triage" or gates[0].qtype != QuestionType.NOUL:
        raise JevEvaluationError(
            "Attestation catalog must have exactly one polarity: safe question, "
            "a triage-tier noul (the fast-path gate)"
        )
    return AttestationCatalog(version=version, questions=questions)


def build_state(intent: Intent, *, max_chars: int) -> str:
    """Build the redacted intent state sent to Jev.

    A structured tool call is included as its operation plus the exact
    arguments the council reviews, so Jev judges the same request the members
    do. State over ``max_chars`` is refused, never truncated: a cut-off tail is
    where a payload would hide, and the chairman and fast path act on this.
    """
    salient: dict[str, Any] = {
        "intent_id": intent.intent_id,
        "agent_id": intent.agent_id,
        "action_type": intent.action_type,
        "description": intent.description,
        "target_resource": intent.target_resource,
        "command": intent.command,
        "command_type": str(intent.command_type),
        "working_directory": intent.working_directory,
        "target_host": intent.target_host,
        "target_environment": str(intent.target_environment or ""),
        "target_cluster": intent.target_cluster,
        "target_namespace": intent.target_namespace,
        "target_container": intent.target_container,
        "context": intent.context,
        "justification": intent.justification,
        "blast_radius": intent.blast_radius,
        "preceding_commands": list(intent.preceding_commands),
        "context_files": {name: content for name, content in intent.context_files.items()},
        "has_rollback_plan": intent.rollback_plan is not None,
    }
    if intent.operation is not None:
        salient["operation"] = intent.operation.model_dump(mode="json")
        material = intent._tool_review_material
        salient["tool_arguments"] = material.arguments if material is not None else None
    serialized = json.dumps(salient, ensure_ascii=False, sort_keys=True)
    scrubbed = redact(serialized)
    if len(scrubbed) > max_chars:
        raise JevEvaluationError(f"Jev state is {len(scrubbed)} chars, over the {max_chars} cap")
    return scrubbed


def build_request(intent: Intent, catalog: AttestationCatalog, config: JevConfig) -> dict[str, Any]:
    """Build the backend POST body: {model, state, questions}.

    The catalog is backend-neutral and always carries criteria; this adapter
    decides what goes on the wire. ``score`` sends its ordered list and
    ``choice`` its object on both providers. ``noul`` criteria are the two-key
    {"true": ..., "false": ...} mapping: openjev accepts them, hosted Jev does
    not (log 2026-09-21), so they are dropped on the typesafe path.
    """
    drop_noul_criteria = config.provider == JevProvider.TYPESAFE
    questions: dict[str, Any] = {}
    for question in catalog.questions:
        spec: dict[str, Any] = {"type": question.qtype, "instructions": question.instructions}
        criteria = question.criteria
        if drop_noul_criteria and question.qtype == QuestionType.NOUL.value:
            criteria = None
        if criteria is not None:
            spec["criteria"] = criteria
        questions[question.id] = spec
    return {
        "model": config.model_version,
        "state": build_state(intent, max_chars=config.max_state_chars),
        "questions": questions,
    }


def _answers_map(payload: dict[str, Any], catalog: AttestationCatalog) -> dict[str, Any]:
    """Locate the per-question answers, tolerating a few response envelopes."""
    for key in ("answers", "results", "questions"):
        candidate = payload.get(key)
        if isinstance(candidate, dict):
            return candidate
    # Otherwise assume answers are keyed by question id at the top level.
    ids = {q.id for q in catalog.questions}
    return {k: v for k, v in payload.items() if k in ids}


def _score_levels(answer: dict[str, Any]) -> int:
    """Number of levels in a score answer, from its legend/probabilities map."""
    for key in ("legend", "probabilities"):
        mapping = answer.get(key)
        if isinstance(mapping, dict) and mapping:
            return len(mapping)
    return 0


def _extract_value(qtype: QuestionType, answer: dict[str, Any]) -> float | str:
    """Pull the typed value from one answer object.

    noul   -> P(true) in 0..1 (field ``noul``).
    choice -> the chosen option string (field ``choice``).
    score  -> Jev returns a level index in 0..N-1 (field ``score``). We normalize
              it to 0..1 as ``score / (levels - 1)`` so a graded answer is
              directly comparable with noul probabilities across the vector; the
              raw index and its label are preserved in the attestation ``detail``.
    """
    if qtype == QuestionType.CHOICE.value:
        choice = answer.get("choice")
        return str(choice) if choice is not None else ""
    raw = answer.get(qtype)
    if raw is None:
        raw = answer.get("value")
    if raw is None:
        raise JevEvaluationError(f"Jev answer has no {qtype} value: {answer!r}")
    try:
        number = float(raw)
    except (TypeError, ValueError) as exc:
        raise JevEvaluationError(f"Jev answer has no numeric {qtype} value: {answer!r}") from exc
    if qtype == QuestionType.SCORE.value:
        levels = _score_levels(answer)
        if levels > 1:
            return max(0.0, min(1.0, number / (levels - 1)))
    return number


def _score_detail(answer: dict[str, Any]) -> dict[str, Any]:
    """Preserve the raw level index, its label, and the distribution for a score."""
    raw_legend = answer.get("legend")
    legend: dict[str, Any] = raw_legend if isinstance(raw_legend, dict) else {}
    raw = answer.get("score")
    label = None
    if raw is not None:
        try:
            label = legend.get(str(round(float(raw))))
        except (TypeError, ValueError):
            pass
    detail: dict[str, Any] = {"raw_score": raw, "levels": len(legend) or None, "label": label}
    if isinstance(answer.get("probabilities"), dict):
        detail["probabilities"] = answer["probabilities"]
    return detail


def _extract_confidence(
    qtype: QuestionType, answer: dict[str, Any], value: float | str
) -> tuple[float, str]:
    """Return the answer's confidence and how it was obtained.

    A real ``confidence`` field is used whenever the response carries one.
    Confirmed against hosted Jev: ``score`` and ``choice`` answers carry it
    (0..1) and ``noul`` answers do not; whether openjev returns one for noul is
    not yet established, so this adapts per answer rather than per provider.

    When it is absent we fall back to a certainty proxy (distance from maximal
    uncertainty, |2p-1|). That proxy is NOT a vendor-calibrated confidence — it
    is derived from the probability it would qualify — so the source is returned
    alongside it and recorded on the attestation, and no threshold should treat
    the two as interchangeable.
    """
    raw = answer.get("confidence")
    if raw is not None:
        try:
            return max(0.0, min(1.0, float(raw))), CONFIDENCE_REPORTED
        except (TypeError, ValueError):
            pass
    if qtype in (QuestionType.NOUL.value, QuestionType.SCORE.value) and isinstance(value, float):
        return max(0.0, min(1.0, abs(2.0 * value - 1.0))), CONFIDENCE_CERTAINTY_PROXY
    return 0.0, CONFIDENCE_UNAVAILABLE


def parse_response(
    payload: dict[str, Any],
    *,
    catalog: AttestationCatalog,
    requested_version: str,
    latency_ms: float,
) -> AttestationVector:
    """Normalize a Jev response into an ``AttestationVector``."""
    model_version = str(payload.get("model") or requested_version)
    answers = _answers_map(payload, catalog)

    attestations: list[Attestation] = []
    for question in catalog.questions:
        answer = answers.get(question.id)
        if not isinstance(answer, dict):
            raise JevEvaluationError(f"Jev response missing answer for question {question.id!r}")
        value = _extract_value(question.qtype, answer)
        confidence, confidence_source = _extract_confidence(question.qtype, answer, value)
        detail: dict[str, Any] = (
            _score_detail(answer) if question.qtype == QuestionType.SCORE.value else {}
        )
        if question.qtype == QuestionType.CHOICE.value and isinstance(
            answer.get("probabilities"), dict
        ):
            detail["probabilities"] = answer["probabilities"]
        detail["confidence_source"] = confidence_source
        attestations.append(
            Attestation(
                question_id=question.id,
                category=question.category,
                qtype=question.qtype,
                value=value,
                confidence=confidence,
                action=question.action,
                tier=question.tier,
                polarity=question.polarity,
                detail=detail,
            )
        )
    usage = payload.get("usage")
    return AttestationVector(
        model_version=model_version,
        catalog_version=catalog.version,
        attestations=attestations,
        latency_ms=latency_ms,
        evaluated_at=datetime.now(UTC),
        outcome="pass",
        usage=usage if isinstance(usage, dict) else {},
    )


def _batch_questions(questions: dict[str, Any], size: int | None) -> list[dict[str, Any]]:
    if size is None or len(questions) <= size:
        return [questions]
    items = list(questions.items())
    return [dict(items[i : i + size]) for i in range(0, len(items), size)]


def _merge_payloads(payloads: list[dict[str, Any]], catalog: AttestationCatalog) -> dict[str, Any]:
    """Combine batched responses into one ``{model, answers, usage}`` payload."""
    if len(payloads) == 1:
        return payloads[0]
    answers: dict[str, Any] = {}
    usage: dict[str, int] = {}
    for payload in payloads:
        answers.update(_answers_map(payload, catalog))
        for key, value in (payload.get("usage") or {}).items():
            if isinstance(value, int):
                usage[key] = usage.get(key, 0) + value
    return {"model": payloads[0].get("model"), "answers": answers, "usage": usage}


class JevClient:
    """Async wrapper around the Jev System One REST endpoint."""

    def __init__(self, config: JevConfig, catalog: AttestationCatalog | None = None):
        self._config = config
        self._catalog = catalog or load_catalog(config.catalog_path)

    @property
    def catalog(self) -> AttestationCatalog:
        return self._catalog

    async def evaluate(self, intent: Intent) -> AttestationVector:
        """Produce the attestation vector for one intent.

        A provider with a per-request question cap gets the catalog in
        concurrent batches; the answers are merged into one response before
        parsing, so any failed batch fails the whole evaluation.
        """
        request_body = build_request(intent, self._catalog, self._config)
        start = time.perf_counter()
        batches = _batch_questions(request_body["questions"], self._config.question_batch_size)
        payloads = await asyncio.gather(
            *(
                asyncio.to_thread(self._post, {**request_body, "questions": questions})
                for questions in batches
            )
        )
        payload = _merge_payloads(list(payloads), self._catalog)
        latency_ms = (time.perf_counter() - start) * 1000
        return parse_response(
            payload,
            catalog=self._catalog,
            requested_version=self._config.model_version,
            latency_ms=latency_ms,
        )

    def _post(self, request_body: dict[str, Any]) -> dict[str, Any]:
        if not self._config.api_key:
            raise JevEvaluationError(
                "Jev api_key is not configured for provider "
                f"{self._config.provider} (set {self._config.api_key_env})"
            )
        body = json.dumps(request_body, separators=(",", ":")).encode("utf-8")
        url = f"{self._config.base_url}{self._config.endpoint_path}"
        request = urllib.request.Request(  # noqa: S310 - URL is operator-configured Jev endpoint.
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self._config.api_key}",
            },
            method="POST",
        )
        timeout_seconds = self._config.timeout_ms / 1000
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise JevEvaluationError(f"Jev returned HTTP {exc.code}: {details}") from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            raise JevEvaluationError(f"Jev request failed: {exc}") from exc

        try:
            decoded = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise JevEvaluationError(f"Jev returned invalid JSON: {exc}") from exc
        if not isinstance(decoded, dict):
            raise JevEvaluationError("Jev response must be a JSON object")
        return decoded
