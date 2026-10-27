# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Decisions derived from the member-zero Jev vector.

Two consumers read the same vector, so no second Jev call is ever made:

* ``chairman_decision`` — the risk level and voting threshold for
  ``chairman.provider: jev``.
* ``evaluate_fast_path`` — whether a read-only intent may get a writ without
  convening the council (opt-in).

Both are strict about the answers they act on: a missing, mistyped or
out-of-range answer raises ``JevRoutingError``, which callers turn into the safe
outcome (unanimous / no fast path). The agent writes the intent's prose, so
every relaxation requires all of its conditions, never a majority of them.
"""

from dataclasses import dataclass, field
from typing import Any

from nomos.config.settings import JevChairmanConfig, JevFastPathConfig
from nomos.level_0.types import Attestation, AttestationVector
from nomos.llm.council_agent import ChairmanDecision

RISK_QUESTION = "risk_level"
RISK_LEVELS = ("low", "medium", "high", "critical")


class JevRoutingError(RuntimeError):
    """The vector lacks an answer a routing decision depends on."""


def _unit(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0.0 <= value <= 1.0:
        raise JevRoutingError(f"{what} is not a probability: {value!r}")
    return float(value)


def _risk(vector: AttestationVector) -> tuple[str, float]:
    answer = vector.by_id(RISK_QUESTION)
    if answer is None:
        raise JevRoutingError(f"vector has no {RISK_QUESTION!r} answer")
    if answer.value not in RISK_LEVELS:
        raise JevRoutingError(f"unknown risk_level {answer.value!r}")
    if answer.detail.get("confidence_source") != "reported":
        raise JevRoutingError("risk_level confidence was not reported by the backend")
    return str(answer.value), _unit(answer.confidence, "risk_level confidence")


def _gate(vector: AttestationVector) -> Attestation:
    gates = [a for a in vector.attestations if a.tier == "triage" and a.polarity == "safe"]
    if len(gates) != 1:
        raise JevRoutingError(f"vector has {len(gates)} triage gates, expected 1")
    return gates[0]


def _weights(vector: AttestationVector) -> list[Attestation]:
    return [a for a in vector.attestations if a.tier == "triage" and a.polarity == "adverse"]


def chairman_decision(vector: AttestationVector, config: JevChairmanConfig) -> ChairmanDecision:
    """Risk level and threshold from the vector. Quorum only when every condition holds.

    An unsure low or medium answer is reported as high, because the risk level
    also sets the writ TTL and an unsure "low" would grant the longest one.
    """
    answered, confidence = _risk(vector)
    risk_level = answered
    if answered in ("low", "medium") and confidence < config.min_risk_confidence:
        risk_level = "high"
    gate = _gate(vector)
    p_read_only = _unit(gate.value, gate.question_id)
    relax = (
        risk_level == "low"
        and confidence >= config.quorum_min_confidence
        and p_read_only >= config.quorum_min_read_only
    )
    threshold = "quorum" if relax else "unanimous"
    return ChairmanDecision(
        threshold=threshold,
        risk_level=risk_level,
        reasoning=(
            f"Jev {vector.model_version}: risk={answered} (confidence {confidence:.2f})"
            + (f", reported as {risk_level} (low confidence)" if risk_level != answered else "")
            + f", p({gate.question_id})={p_read_only:.2f} -> {threshold}."
        ),
    )


@dataclass(frozen=True)
class FastPathResult:
    """Outcome of the fast-path check, recorded in the ledger whether or not it was used."""

    enabled: bool
    eligible: bool
    failed: list[str] = field(default_factory=list)
    values: dict[str, Any] = field(default_factory=dict)

    @property
    def approved(self) -> bool:
        return self.enabled and self.eligible

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "eligible": self.eligible,
            "approved": self.approved,
            "failed": list(self.failed),
            "values": dict(self.values),
        }


def evaluate_fast_path(
    vector: AttestationVector | None,
    config: JevFastPathConfig,
    *,
    authenticated: bool,
    opa_allowed: bool,
    policy_allows: bool,
    floor_fired: bool,
    has_packaged_files: bool,
) -> FastPathResult:
    """Check every fast-path condition.

    Evaluated even when the fast path is disabled, so the ledger carries a
    shadow record ("would have fast-pathed") to calibrate thresholds from.
    """
    failed: list[str] = []
    values: dict[str, Any] = {
        "min_read_only": config.min_read_only,
        "max_danger": config.max_danger,
        "min_risk_confidence": config.min_risk_confidence,
    }
    if not authenticated:
        failed.append("agent not authenticated")
    if not opa_allowed:
        failed.append("OPA did not run and allow")
    if not policy_allows:
        failed.append("council policy rule requires review")
    if floor_fired:
        failed.append("deterministic threshold floor fired")
    if has_packaged_files:
        failed.append("packaged files require code review")

    if vector is None:
        failed.append("no Jev vector")
    else:
        try:
            gate = _gate(vector)
            p_read_only = _unit(gate.value, gate.question_id)
            values[gate.question_id] = p_read_only
            if p_read_only < config.min_read_only:
                failed.append(f"{gate.question_id} {p_read_only:.2f} < {config.min_read_only:.2f}")
            for weight in _weights(vector):
                danger = _unit(weight.value, weight.question_id)
                values[weight.question_id] = danger
                if danger > config.max_danger:
                    failed.append(f"{weight.question_id} {danger:.2f} > {config.max_danger:.2f}")
            risk_level, confidence = _risk(vector)
            values[RISK_QUESTION] = {"choice": risk_level, "confidence": confidence}
            if risk_level != "low":
                failed.append(f"risk_level is {risk_level}")
            elif confidence < config.min_risk_confidence:
                failed.append(
                    f"risk_level confidence {confidence:.2f} < {config.min_risk_confidence:.2f}"
                )
        except JevRoutingError as exc:
            failed.append(str(exc))

    return FastPathResult(enabled=config.enabled, eligible=not failed, failed=failed, values=values)
