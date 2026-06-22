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
Tests for Security Council.

Following Lakos: integration tests using all lower-level components.
"""

import pytest

from nomos.level_0.types import AgentRole, CouncilMember, Intent
from nomos.level_3.executor import LogHandler, NoOpHandler
from nomos.level_4.council import CouncilConfig, SecurityCouncil
from nomos.level_4.council.workflow import WorkflowState


class TestSecurityCouncil:
    """Integration tests for SecurityCouncil."""

    @pytest.fixture
    def council(self):
        config = CouncilConfig(
            council_id="test-council",
            default_token_ttl_seconds=300,
            require_justification=True,
        )
        council = SecurityCouncil(config=config)
        council.register_action_handler(NoOpHandler())
        council.register_action_handler(LogHandler())
        council.register_agent("agent-1", AgentRole.APPLICATION)
        return council

    @pytest.fixture
    def members(self, council):
        m1 = CouncilMember(member_id="alice", public_key_hex="abc", display_name="Alice", weight=1)
        m2 = CouncilMember(member_id="bob", public_key_hex="def", display_name="Bob", weight=1)
        council.register_council_member(m1)
        council.register_council_member(m2)
        return [m1, m2]

    def test_submit_low_risk_intent(self, council):
        intent = Intent.create(
            agent_id="agent-1",
            action_type="system.noop",
            description="Simple no-op action for testing",
            target_resource="/tmp/test",
            justification="Testing the system",
        )
        result = council.submit_intent(intent)

        assert result.success is True
        assert result.workflow is not None
        assert result.workflow.state in (WorkflowState.APPROVED, WorkflowState.AWAITING_APPROVAL)

    def test_submit_requires_justification(self, council):
        intent = Intent.create(
            agent_id="agent-1",
            action_type="system.noop",
            description="No justification provided for this action",
            target_resource="/tmp/test",
        )
        result = council.submit_intent(intent)

        assert result.success is False
        assert "justification" in result.error.lower()

    def test_submit_high_risk_requires_approval(self, council, members):
        intent = Intent.create(
            agent_id="agent-1",
            action_type="database.delete",
            description="Delete production database",
            target_resource="production/main-db",
            justification="Migration complete",
            blast_radius="All production data",
        )
        result = council.submit_intent(intent)

        assert result.success is True
        assert result.requires_approval is True
        assert result.workflow.state == WorkflowState.AWAITING_APPROVAL

    def test_approval_workflow(self, council, members):
        intent = Intent.create(
            agent_id="agent-1",
            action_type="secrets.read",
            description="Read API keys",
            target_resource="/secrets/api-keys",
            justification="Deployment",
        )
        submit_result = council.submit_intent(intent)
        assert submit_result.requires_approval is True

        approve_result = council.approve_intent(intent.intent_id, "alice")
        assert approve_result.success is True

        approve_result2 = council.approve_intent(intent.intent_id, "bob")
        workflow = approve_result2.workflow
        assert workflow.state == WorkflowState.APPROVED

    def test_denial_workflow(self, council, members):
        intent = Intent.create(
            agent_id="agent-1",
            action_type="secrets.delete",
            description="Delete all secrets",
            target_resource="/secrets/*",
            justification="Cleanup",
        )
        council.submit_intent(intent)

        council.deny_intent(intent.intent_id, "alice", reason="Too risky")
        deny_result2 = council.deny_intent(intent.intent_id, "bob", reason="Agreed")

        workflow = deny_result2.workflow
        assert workflow.state == WorkflowState.DENIED

    def test_execute_approved_intent(self, council):
        intent = Intent.create(
            agent_id="agent-1",
            action_type="system.noop",
            description="Execute no-op action for testing",
            target_resource="/test",
            justification="Testing execution",
        )
        submit_result = council.submit_intent(intent)

        if submit_result.workflow.state == WorkflowState.APPROVED:
            execute_result = council.execute_intent(intent.intent_id)
            assert execute_result.success is True
            assert execute_result.workflow.state == WorkflowState.COMPLETED

    def test_full_workflow_with_approval(self, council, members):
        intent = Intent.create(
            agent_id="agent-1",
            action_type="system.log",
            description="Log something to production logs",
            target_resource="production/logs",
            justification="Audit logging",
        )

        submit_result = council.submit_intent(intent)

        if submit_result.requires_approval:
            council.approve_intent(intent.intent_id, "alice")
            council.approve_intent(intent.intent_id, "bob")

        execute_result = council.execute_intent(intent.intent_id)

        assert execute_result.workflow.state in (WorkflowState.COMPLETED, WorkflowState.VERIFIED)

    def test_get_workflow(self, council):
        intent = Intent.create(
            agent_id="agent-1",
            action_type="system.noop",
            description="Test workflow retrieval functionality",
            target_resource="/test",
            justification="Testing",
        )
        council.submit_intent(intent)

        workflow = council.get_workflow(intent.intent_id)
        assert workflow is not None
        assert workflow.intent_id == intent.intent_id

    def test_ledger_validation(self, council):
        intent = Intent.create(
            agent_id="agent-1",
            action_type="system.noop",
            description="Test ledger validation functionality",
            target_resource="/test",
            justification="Testing",
        )
        council.submit_intent(intent)

        assert council.validate_ledger() is True

    def test_ledger_status(self, council):
        intent = Intent.create(
            agent_id="agent-1",
            action_type="system.noop",
            description="Test ledger status functionality",
            target_resource="/test",
            justification="Testing",
        )
        council.submit_intent(intent)

        status = council.get_ledger_status()
        assert status["length"] > 0
        assert status["is_valid"] is True
