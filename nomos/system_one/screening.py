# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Pure policy routing for explicitly selected System One yes/no answers.

The model supplies probabilities; operator configuration supplies consequences.
Existing catalog ``action`` labels remain informational. Screening cannot grant
execution, mint a writ or substitute for an authenticated human decision. Its
``continue`` result only permits the existing authorization flow to continue.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from nomos.config.settings import SystemOneScreeningConfig

if TYPE_CHECKING:
    from nomos.level_0.types import AttestationVector
    from nomos.system_one.client import AttestationCatalog

ScreeningAction = Literal["deny", "requires_human_approval"]
ScreeningDecision = Literal["continue", "denied", "requires_human_approval", "error"]
ScreeningMode = Literal["shadow", "enforce"]


@dataclass(frozen=True)
class SelectedScreeningRule:
    """A configured rule with its inherited action resolved once per evaluation."""

    threshold: float
    action: ScreeningAction


@dataclass(frozen=True)
class ScreeningRuleResult:
    """The numeric comparison recorded for one valid, selected answer."""

    question_id: str
    threshold: float
    probability: float
    action: ScreeningAction
    matched: bool

    def to_dict(self) -> dict[str, Any]:
        """Return only bounded policy facts, never input text or model reasoning."""
        return {
            "question_id": self.question_id,
            "threshold": self.threshold,
            "probability": self.probability,
            "action": self.action,
            "matched": self.matched,
        }


@dataclass(frozen=True)
class ScreeningResult:
    """A screening verdict and audit facts, including non-enforcing shadow results.

    An error describes unusable required answers, not a model verdict. Enforce
    callers must stop on that result; shadow callers retain normal behavior.
    """

    decision: ScreeningDecision
    mode: ScreeningMode
    evaluated: tuple[ScreeningRuleResult, ...] = ()
    error: str = ""

    @property
    def matched(self) -> tuple[ScreeningRuleResult, ...]:
        """Return the rules whose probability reached the configured threshold."""
        return tuple(rule for rule in self.evaluated if rule.matched)

    @property
    def enforced_decision(self) -> ScreeningDecision:
        """Shadow observations never change the existing authorization path."""
        return self.decision if self.mode == "enforce" else "continue"

    def to_dict(self) -> dict[str, Any]:
        """Serialize the observed verdict separately from its enforcement effect."""
        return {
            "decision": self.decision,
            "mode": self.mode,
            "enforced_decision": self.enforced_decision,
            "evaluated": [rule.to_dict() for rule in self.evaluated],
            "matched": [rule.question_id for rule in self.matched],
            "error": self.error,
        }


def selected_screening_rules(
    config: SystemOneScreeningConfig, *, policy_only: bool = False
) -> dict[str, SelectedScreeningRule]:
    """Resolve actions and select only deny rules for human-approved replay.

    The host already verified approval for the exact operation on replay, so
    ask rules must not prompt again. Defaulted deny rules still apply. This
    returns new immutable rules and never changes shared configuration.
    """
    if not config.enabled:
        return {}
    selected: dict[str, SelectedScreeningRule] = {}
    for question_id, rule in config.rules.items():
        action = rule.action if rule.action is not None else config.default_action
        if not policy_only or action == "deny":
            selected[question_id] = SelectedScreeningRule(rule.threshold, action)
    return selected


def validate_screening_catalog(
    catalog: AttestationCatalog, config: SystemOneScreeningConfig
) -> None:
    """Validate selected policy questions against the client's effective catalog.

    Lookup occurs after evidence filtering, so an unasked evidence question
    cannot masquerade as an enforced control. Only adverse yes/no questions
    have the unambiguous P(violation) semantics this first policy supports.
    """
    if not config.enabled:
        return
    questions = {question.id: question for question in catalog.questions}
    for question_id in config.rules:
        question = questions.get(question_id)
        if question is None:
            raise ValueError(f"Screening question {question_id!r} is not in the effective catalog")
        if question.qtype != "noul" or question.polarity != "adverse":
            raise ValueError(
                f"Screening question {question_id!r} must be an adverse yes/no (noul) question"
            )


def evaluate_screening(
    vector: AttestationVector | None,
    config: SystemOneScreeningConfig,
    *,
    policy_only: bool = False,
) -> ScreeningResult:
    """Compare selected probabilities and apply deny-before-ask precedence.

    A match is inclusive (probability >= threshold). Every selected answer must
    be present exactly once, adverse, yes/no and a finite numeric probability.
    Missing or malformed required answers produce an error, never an implicit
    negative answer or a new human-approval request. Unselected informational
    answers do not acquire policy authority from their catalog action labels.
    """
    rules = selected_screening_rules(config, policy_only=policy_only)
    if not rules:
        return ScreeningResult("continue", config.mode)
    if vector is None:
        return ScreeningResult("error", config.mode, error="Screening answers are unavailable.")

    answers: dict[str, Any] = {}
    for item in vector.attestations:
        if item.question_id not in rules:
            continue
        if item.question_id in answers:
            return ScreeningResult(
                "error", config.mode, error="Screening contains a duplicate required answer."
            )
        answers[item.question_id] = item

    evaluated: list[ScreeningRuleResult] = []
    for question_id, rule in rules.items():
        answer = answers.get(question_id)
        if answer is None:
            return ScreeningResult(
                "error", config.mode, tuple(evaluated), "Screening is missing a required answer."
            )
        value = answer.value
        if (
            answer.qtype != "noul"
            or answer.polarity != "adverse"
            # Shadow keeps the legacy parsed vector for existing consumers,
            # but a coercible raw answer cannot clear an enforced policy.
            or answer.detail.get("screening_answer_valid") is False
            or isinstance(value, bool)
            or not isinstance(value, int | float)
            or not 0.0 <= value <= 1.0
        ):
            return ScreeningResult(
                "error", config.mode, tuple(evaluated), "Screening contains an invalid answer."
            )
        evaluated.append(
            ScreeningRuleResult(
                question_id, rule.threshold, float(value), rule.action, value >= rule.threshold
            )
        )

    matched = {rule.action for rule in evaluated if rule.matched}
    decision: ScreeningDecision = "continue"
    if "deny" in matched:
        decision = "denied"
    elif "requires_human_approval" in matched:
        decision = "requires_human_approval"
    return ScreeningResult(decision, config.mode, tuple(evaluated))
