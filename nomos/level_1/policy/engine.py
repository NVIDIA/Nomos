# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


"""
Policy engine: evaluates intents against explicit rules.

Rules determine ALLOW, DENY, or REQUIRE_QUORUM. No heuristic scoring.
The quorum of council agents makes judgment calls.
"""

from dataclasses import dataclass

from nomos.level_0.types import AgentRole, Intent

from .rules import DecisionType, PolicyDecision, PolicyRuleSet


@dataclass
class EvaluationResult:
    """Result of policy evaluation."""

    intent_id: str
    decision: PolicyDecision
    explanation: str

    def is_approved(self) -> bool:
        return self.decision.decision == DecisionType.ALLOW

    def requires_approval(self) -> bool:
        return self.decision.decision in (
            DecisionType.REQUIRE_APPROVAL,
            DecisionType.REQUIRE_QUORUM,
        )

    def to_dict(self) -> dict:
        return {
            "intent_id": self.intent_id,
            "decision": self.decision.to_dict(),
            "explanation": self.explanation,
        }


class PolicyEngine:
    """
    Evaluates intents against policy rules.

    Rules explicitly say ALLOW, DENY, or REQUIRE_QUORUM.
    No heuristic risk scoring - the quorum makes judgment calls.
    """

    def __init__(self, rules: PolicyRuleSet | None = None):
        self._rules = rules or PolicyRuleSet.default_rules()

    @property
    def rules(self) -> PolicyRuleSet:
        return self._rules

    def evaluate(
        self,
        intent: Intent,
        agent_role: AgentRole = AgentRole.APPLICATION,
    ) -> EvaluationResult:
        """
        Evaluate an intent against policy rules.

        Returns the rule decision directly - no heuristic escalation.
        """
        decision = self._rules.evaluate(intent, agent_role)

        explanation = f"Rule '{decision.rule_id}': {decision.reason}"

        return EvaluationResult(
            intent_id=intent.intent_id,
            decision=decision,
            explanation=explanation,
        )

    def add_rule(self, rule) -> None:
        """Add a policy rule."""
        self._rules.add(rule)

    def remove_rule(self, rule_id: str) -> bool:
        """Remove a policy rule."""
        return self._rules.remove(rule_id)
