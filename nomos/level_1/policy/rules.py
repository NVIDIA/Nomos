# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Policy rules: declarative constraints for intent evaluation.

Rules define what actions are permitted, denied, or require escalation.
"""

import fnmatch
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from nomos.level_0.types import AgentRole, Intent

from .shell import shell_path_policy_error


class DecisionType(Enum):
    """Possible policy decisions."""

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"
    REQUIRE_QUORUM = "require_quorum"
    ESCALATE = "escalate"


@dataclass
class PolicyDecision:
    """Result of policy evaluation."""

    decision: DecisionType
    rule_id: str
    reason: str
    required_approvers: int = 0
    required_quorum_weight: int = 0
    conditions: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_approved(self) -> bool:
        return self.decision == DecisionType.ALLOW

    def requires_approval(self) -> bool:
        return self.decision in (
            DecisionType.REQUIRE_APPROVAL,
            DecisionType.REQUIRE_QUORUM,
            DecisionType.ESCALATE,
        )

    def to_dict(self) -> dict:
        return {
            "decision": self.decision.value,
            "rule_id": self.rule_id,
            "reason": self.reason,
            "required_approvers": self.required_approvers,
            "required_quorum_weight": self.required_quorum_weight,
            "conditions": self.conditions,
            "metadata": self.metadata,
        }


@dataclass
class PolicyRule:
    """
    A single policy rule for intent evaluation.

    Rules are matched in priority order; first match wins.
    """

    rule_id: str
    name: str
    description: str = ""
    priority: int = 100

    action_types: set[str] = field(default_factory=set)
    resource_patterns: list[str] = field(default_factory=list)
    allowed_roles: set[AgentRole] = field(default_factory=set)
    denied_roles: set[AgentRole] = field(default_factory=set)
    decision: DecisionType = DecisionType.ALLOW
    required_approvers: int = 0
    required_quorum_weight: int = 0
    conditions: list[str] = field(default_factory=list)

    enabled: bool = True

    def matches(self, intent: Intent, agent_role: AgentRole = AgentRole.APPLICATION) -> bool:
        """Check if this rule applies to the given intent."""
        if not self.enabled:
            return False

        if self.action_types and intent.action_type not in self.action_types:
            if "*" not in self.action_types:
                return False

        if self.resource_patterns:
            matched = False
            for pattern in self.resource_patterns:
                if fnmatch.fnmatch(intent.target_resource, pattern):
                    matched = True
                    break
            if not matched:
                return False

        if self.denied_roles:
            if agent_role not in self.denied_roles:
                return False
            return True

        if self.allowed_roles and agent_role not in self.allowed_roles:
            return False

        return True

    def evaluate(
        self, intent: Intent, agent_role: AgentRole = AgentRole.APPLICATION
    ) -> PolicyDecision | None:
        """Evaluate the intent against this rule."""
        if not self.matches(intent, agent_role):
            return None

        if self.denied_roles and agent_role in self.denied_roles:
            return PolicyDecision(
                decision=DecisionType.DENY,
                rule_id=self.rule_id,
                reason=f"Role {agent_role.value} denied by rule {self.name}",
            )

        return PolicyDecision(
            decision=self.decision,
            rule_id=self.rule_id,
            reason=f"Matched rule: {self.name}",
            required_approvers=self.required_approvers,
            required_quorum_weight=self.required_quorum_weight,
            conditions=self.conditions.copy(),
        )

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "name": self.name,
            "description": self.description,
            "priority": self.priority,
            "action_types": list(self.action_types),
            "resource_patterns": self.resource_patterns,
            "allowed_roles": [r.value for r in self.allowed_roles],
            "denied_roles": [r.value for r in self.denied_roles],
            "decision": self.decision.value,
            "required_approvers": self.required_approvers,
            "required_quorum_weight": self.required_quorum_weight,
            "conditions": self.conditions,
            "enabled": self.enabled,
        }


class ShellExecutablePathRule(PolicyRule):
    """Deny shell commands that rely on PATH lookup for executables."""

    def matches(self, intent: Intent, agent_role: AgentRole = AgentRole.APPLICATION) -> bool:
        command_type = getattr(intent.command_type, "value", intent.command_type)
        return bool(
            self.enabled
            and intent.command
            and command_type == "shell"
            and shell_path_policy_error(intent.command)
        )

    def evaluate(
        self, intent: Intent, agent_role: AgentRole = AgentRole.APPLICATION
    ) -> PolicyDecision | None:
        if not self.matches(intent, agent_role):
            return None

        return PolicyDecision(
            decision=DecisionType.DENY,
            rule_id=self.rule_id,
            reason=shell_path_policy_error(intent.command),
        )


class PolicyRuleSet:
    """
    Ordered collection of policy rules.

    Rules are evaluated in priority order (lower number = higher priority).
    """

    def __init__(self):
        self._rules: list[PolicyRule] = []
        self._rules_by_id: dict[str, PolicyRule] = {}

    def add(self, rule: PolicyRule) -> None:
        """Add a rule to the set."""
        if rule.rule_id in self._rules_by_id:
            raise ValueError(f"Rule {rule.rule_id} already exists")
        self._rules.append(rule)
        self._rules_by_id[rule.rule_id] = rule
        self._rules.sort(key=lambda r: r.priority)

    def remove(self, rule_id: str) -> bool:
        """Remove a rule by ID."""
        rule = self._rules_by_id.pop(rule_id, None)
        if rule:
            self._rules.remove(rule)
            return True
        return False

    def get(self, rule_id: str) -> PolicyRule | None:
        """Get a rule by ID."""
        return self._rules_by_id.get(rule_id)

    def evaluate(
        self, intent: Intent, agent_role: AgentRole = AgentRole.APPLICATION
    ) -> PolicyDecision:
        """
        Evaluate intent against all rules.

        Returns first matching decision, or default allow.
        """
        for rule in self._rules:
            decision = rule.evaluate(intent, agent_role)
            if decision is not None:
                return decision

        return PolicyDecision(
            decision=DecisionType.REQUIRE_APPROVAL,
            rule_id="default",
            reason="No matching rules; requires council approval",
        )

    def __len__(self) -> int:
        return len(self._rules)

    def __iter__(self):
        return iter(self._rules)

    @classmethod
    def default_rules(cls) -> "PolicyRuleSet":
        """Create a rule set with sensible defaults."""
        ruleset = cls()

        ruleset.add(
            PolicyRule(
                rule_id="deny-anonymous",
                name="Deny Anonymous",
                description="Anonymous agents cannot perform any actions",
                priority=1,
                action_types={"*"},
                denied_roles={AgentRole.ANONYMOUS},
                decision=DecisionType.DENY,
            )
        )

        ruleset.add(
            PolicyRule(
                rule_id="require-approval-secrets",
                name="Secrets Require Approval",
                description="Any secrets access requires quorum approval",
                priority=10,
                action_types={"secrets.read", "secrets.write", "secrets.delete"},
                decision=DecisionType.REQUIRE_APPROVAL,
                required_approvers=1,
            )
        )

        ruleset.add(
            PolicyRule(
                rule_id="require-quorum-prod",
                name="Production Changes Require Quorum",
                description="Production environment changes require multi-party approval",
                priority=20,
                resource_patterns=["prod/*", "production/*", "*/prod/*"],
                decision=DecisionType.REQUIRE_QUORUM,
                required_quorum_weight=2,
            )
        )

        ruleset.add(
            PolicyRule(
                rule_id="allow-read-operations",
                name="Allow Read Operations",
                description="Read-only operations are generally allowed",
                priority=50,
                action_types={"read", "list", "get", "describe", "query"},
                decision=DecisionType.ALLOW,
            )
        )

        return ruleset
