# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for policy module.

Following Lakos: test policy engine using Level 0 types.
"""

from nomos.level_0.types import (
    AgentRole,
    Intent,
)
from nomos.level_1.policy import (
    DecisionType,
    PolicyEngine,
    PolicyRule,
    PolicyRuleSet,
)


class TestPolicyRule:
    """Tests for PolicyRule."""

    def test_matches_action_type(self):
        rule = PolicyRule(
            rule_id="test",
            name="Test Rule",
            action_types={"file.delete"},
        )
        intent = Intent.create(
            agent_id="agent-1",
            action_type="file.delete",
            description="Delete temporary file for testing",
            target_resource="/tmp/test",
        )
        assert rule.matches(intent) is True

    def test_no_match_wrong_action(self):
        rule = PolicyRule(
            rule_id="test",
            name="Test Rule",
            action_types={"file.delete"},
        )
        intent = Intent.create(
            agent_id="agent-1",
            action_type="file.read",
            description="Read temporary file for testing",
            target_resource="/tmp/test",
        )
        assert rule.matches(intent) is False

    def test_matches_resource_pattern(self):
        rule = PolicyRule(
            rule_id="test",
            name="Test Rule",
            resource_patterns=["prod/*"],
        )
        intent = Intent.create(
            agent_id="agent-1",
            action_type="deploy.application",
            description="Deploy application to production",
            target_resource="prod/app",
        )
        assert rule.matches(intent) is True

    def test_denied_role(self):
        rule = PolicyRule(
            rule_id="test",
            name="Deny Anonymous",
            denied_roles={AgentRole.ANONYMOUS},
            decision=DecisionType.DENY,
        )
        intent = Intent.create(
            agent_id="agent-1",
            action_type="file.read",
            description="Test reading a file for validation",
            target_resource="/test",
        )
        decision = rule.evaluate(intent, AgentRole.ANONYMOUS)
        assert decision is not None
        assert decision.decision == DecisionType.DENY


class TestPolicyRuleSet:
    """Tests for PolicyRuleSet."""

    def test_add_and_evaluate(self):
        ruleset = PolicyRuleSet()
        ruleset.add(
            PolicyRule(
                rule_id="deny-all",
                name="Deny All",
                priority=1,
                action_types={"*"},
                decision=DecisionType.DENY,
            )
        )

        intent = Intent.create(
            agent_id="agent-1",
            action_type="file.delete",
            description="Test action for ruleset evaluation",
            target_resource="/test",
        )
        decision = ruleset.evaluate(intent)
        assert decision.decision == DecisionType.DENY

    def test_priority_order(self):
        ruleset = PolicyRuleSet()
        ruleset.add(
            PolicyRule(
                rule_id="allow",
                name="Allow",
                priority=10,
                action_types={"*"},
                decision=DecisionType.ALLOW,
            )
        )
        ruleset.add(
            PolicyRule(
                rule_id="deny",
                name="Deny",
                priority=1,
                action_types={"*"},
                decision=DecisionType.DENY,
            )
        )

        intent = Intent.create(
            agent_id="agent-1",
            action_type="file.read",
            description="Test priority order in ruleset",
            target_resource="/test",
        )
        decision = ruleset.evaluate(intent)
        assert decision.rule_id == "deny"

    def test_default_rules(self):
        ruleset = PolicyRuleSet.default_rules()
        assert len(ruleset) > 0

    def test_default_rules_do_not_own_shell_path_policy(self):
        ruleset = PolicyRuleSet.default_rules()
        intent = Intent.create(
            agent_id="agent-1",
            description="Write a test marker file",
            target_resource="/tmp/marker",
            command="echo test > /tmp/marker",
            justification="Testing shell path policy",
        )

        decision = ruleset.evaluate(intent)

        assert decision.decision == DecisionType.REQUIRE_APPROVAL

    def test_shell_path_rule_can_be_added_explicitly(self):
        from nomos.level_1.policy.rules import ShellExecutablePathRule

        ruleset = PolicyRuleSet()
        ruleset.add(
            ShellExecutablePathRule(
                rule_id="deny-non-absolute-shell-executables",
                name="Deny Non-Absolute Shell Executables",
                description="Shell commands must use absolute executable paths.",
                priority=1,
                decision=DecisionType.DENY,
            )
        )
        intent = Intent.create(
            agent_id="agent-1",
            description="Write a test marker file",
            target_resource="/tmp/marker",
            command="echo test > /tmp/marker",
            justification="Testing shell path policy",
        )

        decision = ruleset.evaluate(intent)

        assert decision.decision == DecisionType.DENY
        assert decision.rule_id == "deny-non-absolute-shell-executables"
        assert "absolute path" in decision.reason

    def test_shell_path_rule_allows_cd_builtin_with_absolute_command(self):
        from nomos.level_1.policy.rules import ShellExecutablePathRule

        ruleset = PolicyRuleSet()
        ruleset.add(
            ShellExecutablePathRule(
                rule_id="deny-non-absolute-shell-executables",
                name="Deny Non-Absolute Shell Executables",
                description="Shell commands must use absolute executable paths.",
                priority=1,
                decision=DecisionType.DENY,
            )
        )
        intent = Intent.create(
            agent_id="agent-1",
            description="Summarize git changes",
            target_resource="/workspace",
            command="cd /workspace && /usr/bin/git diff --stat HEAD",
            justification="Testing shell builtin path policy behavior",
        )

        decision = ruleset.evaluate(intent)

        assert decision.decision == DecisionType.REQUIRE_APPROVAL

    def test_default_rules_allow_absolute_shell_executables_to_continue(self):
        ruleset = PolicyRuleSet.default_rules()
        intent = Intent.create(
            agent_id="agent-1",
            description="Write a test marker file",
            target_resource="/tmp/marker",
            command="/bin/echo test > /tmp/marker",
            justification="Testing shell path policy",
        )

        decision = ruleset.evaluate(intent)

        assert decision.decision == DecisionType.REQUIRE_APPROVAL


class TestPolicyEngine:
    """Tests for PolicyEngine."""

    def test_evaluate_returns_result(self):
        engine = PolicyEngine()
        intent = Intent.create(
            agent_id="agent-1",
            action_type="file.read",
            description="Read file from temporary directory",
            target_resource="/tmp/test",
            justification="Testing",
        )
        result = engine.evaluate(intent)
        assert result.intent_id == intent.intent_id
        assert result.decision is not None

    def test_deny_rule(self):
        rules = PolicyRuleSet()
        rules.add(
            PolicyRule(
                rule_id="deny-test",
                name="Deny Test",
                action_types={"system.dangerous"},
                decision=DecisionType.DENY,
            )
        )
        engine = PolicyEngine(rules=rules)

        intent = Intent.create(
            agent_id="agent-1",
            action_type="system.dangerous",
            description="Dangerous action that should be denied",
            target_resource="/test",
        )
        result = engine.evaluate(intent)
        assert result.decision.decision == DecisionType.DENY

    def test_quorum_rule(self):
        rules = PolicyRuleSet()
        rules.add(
            PolicyRule(
                rule_id="quorum-prod",
                name="Production Quorum",
                resource_patterns=["production/*"],
                decision=DecisionType.REQUIRE_QUORUM,
                required_quorum_weight=2,
            )
        )
        engine = PolicyEngine(rules=rules)

        intent = Intent.create(
            agent_id="agent-1",
            action_type="deploy.application",
            description="Deploy application to production environment",
            target_resource="production/main-db",
        )
        result = engine.evaluate(intent)
        assert result.decision.decision == DecisionType.REQUIRE_QUORUM
        assert result.requires_approval() is True
