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
Tests for sandbox executor.

Following Lakos: test using components from lower levels.
"""

import pytest

from nomos.level_0.clock import MockClock
from nomos.level_0.types import CapabilityScope, Intent
from nomos.level_1.ledger import LedgerChain
from nomos.level_2.capability import CapabilityService, MintRequest
from nomos.level_3.executor import (
    Action,
    ActionRegistry,
    ExecutionStatus,
    LogHandler,
    NoOpHandler,
    SandboxExecutor,
)


class TestActionRegistry:
    """Tests for ActionRegistry."""

    def test_register_and_get(self):
        registry = ActionRegistry()
        handler = NoOpHandler()
        registry.register(handler)

        assert registry.get("system.noop") is handler
        assert registry.has_handler("system.noop") is True

    def test_list_action_types(self):
        registry = ActionRegistry()
        registry.register(NoOpHandler())
        registry.register(LogHandler())

        types = registry.list_action_types()
        assert "system.noop" in types
        assert "system.log" in types


class TestSandboxExecutor:
    """Tests for SandboxExecutor."""

    @pytest.fixture
    def setup(self):
        clock = MockClock()
        ledger = LedgerChain(clock=clock)
        ledger.initialize(creator_id="test")
        capability_service = CapabilityService(ledger=ledger, clock=clock)

        executor = SandboxExecutor(
            capability_service=capability_service,
            ledger=ledger,
            clock=clock,
        )
        executor.register_handler(NoOpHandler())
        executor.register_handler(LogHandler())

        return {
            "executor": executor,
            "capability_service": capability_service,
            "clock": clock,
        }

    @pytest.fixture
    def valid_token(self, setup):
        intent = Intent.create(
            agent_id="agent-1",
            action_type="system.noop",
            description="No-op test for executor validation",
            target_resource="/test",
        )
        scope = CapabilityScope(
            action_type="system.noop",
            resource_pattern="*",
            allowed_operations=frozenset(["execute"]),
        )
        result = setup["capability_service"].mint(MintRequest(intent=intent, scope=scope))
        return result.token

    def test_execute_with_valid_token(self, setup, valid_token):
        action = Action(
            action_type="system.noop",
            resource="/test",
            operation="execute",
        )
        result = setup["executor"].execute(action, valid_token.token_id)

        assert result.status == ExecutionStatus.SUCCESS
        assert result.output["status"] == "success"

    def test_execute_without_token(self, setup):
        action = Action(
            action_type="system.noop",
            resource="/test",
            operation="execute",
        )
        result = setup["executor"].execute(action, "nonexistent-token")

        assert result.status == ExecutionStatus.INVALID_TOKEN

    def test_execute_wrong_action_type(self, setup, valid_token):
        action = Action(
            action_type="system.log",
            resource="/test",
            operation="execute",
        )
        result = setup["executor"].execute(action, valid_token.token_id)

        assert result.status == ExecutionStatus.INVALID_TOKEN

    def test_execute_no_handler(self, setup):
        intent = Intent.create(
            agent_id="agent-1",
            action_type="system.unknown",
            description="Unknown action type for handler test",
            target_resource="/test",
        )
        scope = CapabilityScope(action_type="system.unknown", resource_pattern="*")
        mint_result = setup["capability_service"].mint(MintRequest(intent=intent, scope=scope))

        action = Action(action_type="system.unknown", resource="/test", operation="execute")
        result = setup["executor"].execute(action, mint_result.token.token_id)

        assert result.status == ExecutionStatus.NO_HANDLER

    def test_execute_consumes_token(self, setup, valid_token):
        action = Action(action_type="system.noop", resource="/test", operation="execute")
        setup["executor"].execute(action, valid_token.token_id)

        token = setup["capability_service"].get(valid_token.token_id)
        assert token.invocation_count == 1

    def test_execution_recorded_in_ledger(self, setup, valid_token):
        initial_length = setup["executor"]._ledger.length()

        action = Action(action_type="system.noop", resource="/test", operation="execute")
        setup["executor"].execute(action, valid_token.token_id)

        assert setup["executor"]._ledger.length() > initial_length
