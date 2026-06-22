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
Tests for capability token service.

Following Lakos: test using Level 0 and Level 1 components.
"""

import pytest

from nomos.level_0.clock import MockClock
from nomos.level_0.types import CapabilityScope, Intent
from nomos.level_1.ledger import LedgerChain
from nomos.level_2.capability import CapabilityService, MintRequest


class TestCapabilityService:
    """Tests for CapabilityService."""

    @pytest.fixture
    def service(self):
        clock = MockClock()
        ledger = LedgerChain(clock=clock)
        ledger.initialize(creator_id="test")
        return CapabilityService(
            ledger=ledger,
            clock=clock,
            issuer_id="test-council",
            default_ttl_seconds=300,
        )

    @pytest.fixture
    def intent(self):
        return Intent.create(
            agent_id="agent-1",
            action_type="file.delete",
            description="Delete file",
            target_resource="/tmp/test.txt",
        )

    def test_mint_creates_token(self, service, intent):
        scope = CapabilityScope(
            action_type="file.delete",
            resource_pattern="/tmp/*",
        )
        result = service.mint(MintRequest(intent=intent, scope=scope))

        assert result.success is True
        assert result.token is not None
        assert result.token.intent_id == intent.intent_id
        assert result.token.issuer_id == "test-council"

    def test_mint_records_in_ledger(self, service, intent):
        scope = CapabilityScope(action_type="file.delete", resource_pattern="*")
        result = service.mint(MintRequest(intent=intent, scope=scope))

        assert result.ledger_sequence is not None
        assert result.ledger_sequence > 0

    def test_validate_valid_token(self, service, intent):
        scope = CapabilityScope(
            action_type="file.delete",
            resource_pattern="/tmp/*",
            allowed_operations=frozenset(["execute"]),
        )
        mint_result = service.mint(MintRequest(intent=intent, scope=scope))
        token = mint_result.token

        validation = service.validate(
            token_id=token.token_id,
            action_type="file.delete",
            resource="/tmp/test.txt",
            operation="execute",
        )
        assert validation.valid is True

    def test_validate_expired_token(self, service, intent):
        scope = CapabilityScope(action_type="file.delete", resource_pattern="*")
        mint_result = service.mint(MintRequest(intent=intent, scope=scope, ttl_seconds=60))

        service._clock.advance(minutes=2)

        validation = service.validate(
            token_id=mint_result.token.token_id,
            action_type="file.delete",
            resource="/tmp/test.txt",
            operation="execute",
        )
        assert validation.valid is False
        assert "expired" in validation.reason.lower()

    def test_validate_wrong_action(self, service, intent):
        scope = CapabilityScope(action_type="file.delete", resource_pattern="*")
        mint_result = service.mint(MintRequest(intent=intent, scope=scope))

        validation = service.validate(
            token_id=mint_result.token.token_id,
            action_type="file.read",
            resource="/tmp/test.txt",
            operation="execute",
        )
        assert validation.valid is False

    def test_consume_increments_count(self, service, intent):
        scope = CapabilityScope(action_type="file.delete", resource_pattern="*", max_invocations=2)
        mint_result = service.mint(MintRequest(intent=intent, scope=scope))
        token_id = mint_result.token.token_id

        assert service.consume(token_id) is True
        token = service.get(token_id)
        assert token.invocation_count == 1

    def test_revoke_makes_token_invalid(self, service, intent):
        scope = CapabilityScope(action_type="file.delete", resource_pattern="*")
        mint_result = service.mint(MintRequest(intent=intent, scope=scope))
        token_id = mint_result.token.token_id

        assert service.revoke(token_id, reason="Manual revocation") is True

        validation = service.validate(
            token_id=token_id,
            action_type="file.delete",
            resource="/tmp/test.txt",
            operation="execute",
        )
        assert validation.valid is False
        assert "revoked" in validation.reason.lower()

    def test_get_active_for_agent(self, service, intent):
        scope = CapabilityScope(action_type="file.delete", resource_pattern="*")
        service.mint(MintRequest(intent=intent, scope=scope))

        active = service.get_active_for_agent("agent-1")
        assert len(active) == 1
