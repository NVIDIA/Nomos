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
Tests for ChairmanAgent.pre_authorize — Phase 2.3.

Tests the two-phase pre-authorization check:
  Phase 1 (deterministic): permission gate
  Phase 2 (LLM): risk assessment (mocked)
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nomos.level_0.types import Intent
from nomos.llm.council_agent import (
    ChairmanAgent,
    ChairmanPreAuthDecision,
)


def _make_intent(
    command: str = "echo hello",
    *,
    action_type: str = "system.info",
    description: str = "Test command for pre-authorization",
    blast_radius: str = "",
    target_environment: str = "development",
) -> Intent:
    return Intent.create(
        agent_id="test-agent",
        action_type=action_type,
        description=description,
        target_resource="/tmp",
        command=command,
        justification="Testing",
        blast_radius=blast_radius,
        target_environment=target_environment,
    )


def _make_chairman(
    threshold: str = "quorum",
    risk_level: str = "low",
    *,
    allow_quorum: bool = False,
) -> ChairmanAgent:
    """Create a ChairmanAgent with a mocked LLM backend."""
    backend = MagicMock()
    response = MagicMock()
    response.content = (
        f'{{"threshold": "{threshold}", "reasoning": "test", "risk_level": "{risk_level}"}}'
    )
    backend.complete = AsyncMock(return_value=response)
    return ChairmanAgent(backend=backend, allow_quorum=allow_quorum)


class TestPermissionGate:
    """Phase 1 — deterministic permission check (no LLM call)."""

    @pytest.mark.asyncio
    async def test_missing_permissions_denied(self):
        """Agent lacking required permissions gets immediate denial."""
        chairman = _make_chairman()
        intent = _make_intent("rm -rf /var/data")

        result = await chairman.pre_authorize(
            intent=intent,
            agent_permissions={"shell:read", "file:read"},
            required_permissions={"shell:read", "shell:write", "file:delete"},
        )

        assert result.authorized is False
        assert result.threshold is None
        assert "file:delete" in result.missing_permissions
        assert "shell:write" in result.missing_permissions
        # LLM should NOT have been called
        chairman._backend.complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_permissions_denied(self):
        """Agent with no permissions gets denied."""
        chairman = _make_chairman()

        result = await chairman.pre_authorize(
            intent=_make_intent(),
            agent_permissions=set(),
            required_permissions={"shell:read"},
        )

        assert result.authorized is False
        assert "shell:read" in result.missing_permissions

    @pytest.mark.asyncio
    async def test_empty_required_always_passes_to_llm(self):
        """No required permissions → passes to Phase 2 (LLM)."""
        chairman = _make_chairman(threshold="quorum", risk_level="low", allow_quorum=True)

        result = await chairman.pre_authorize(
            intent=_make_intent(),
            agent_permissions={"shell:read"},
            required_permissions=set(),
        )

        assert result.authorized is True
        assert result.threshold == "quorum"
        chairman._backend.complete.assert_called_once()


class TestLLMRiskAssessment:
    """Phase 2 — LLM-based risk assessment (backend mocked)."""

    @pytest.mark.asyncio
    async def test_sufficient_permissions_proceeds_to_llm(self):
        """Agent with all required permissions triggers LLM evaluation."""
        chairman = _make_chairman(threshold="quorum", risk_level="low", allow_quorum=True)

        result = await chairman.pre_authorize(
            intent=_make_intent("ls /tmp"),
            agent_permissions={"shell:read", "shell:write", "file:read"},
            required_permissions={"shell:read"},
        )

        assert result.authorized is True
        assert result.threshold == "quorum"
        assert result.risk_level == "low"
        assert result.missing_permissions == []
        chairman._backend.complete.assert_called_once()

    @pytest.mark.asyncio
    async def test_high_risk_gets_unanimous(self):
        """High-risk command gets unanimous threshold from LLM."""
        chairman = _make_chairman(threshold="unanimous", risk_level="critical")

        result = await chairman.pre_authorize(
            intent=_make_intent("kubectl delete namespace production"),
            agent_permissions={"shell:read", "shell:write", "infra:destroy"},
            required_permissions={"shell:read", "infra:destroy"},
        )

        assert result.authorized is True
        assert result.threshold == "unanimous"
        assert result.risk_level == "critical"

    @pytest.mark.asyncio
    async def test_quorum_without_explicit_enablement_becomes_unanimous(self):
        """Quorum is unavailable unless deployment config enables it."""
        chairman = _make_chairman(threshold="quorum", risk_level="low")

        result = await chairman.pre_authorize(
            intent=_make_intent("ls /tmp"),
            agent_permissions={"shell:read", "file:read"},
            required_permissions={"shell:read"},
        )

        assert result.authorized is True
        assert result.threshold == "unanimous"
        assert "not enabled" in result.reasoning

    @pytest.mark.asyncio
    async def test_recursive_delete_over_app_forces_unanimous(self):
        """Broad recursive delete over /app gets a deterministic unanimous floor."""
        chairman = _make_chairman(threshold="quorum", risk_level="low")

        result = await chairman.pre_authorize(
            intent=_make_intent(
                "/usr/bin/find /app -name '*.pyc' -delete",
                action_type="file.delete",
                description="Clean up compiled Python caches",
                blast_radius="Deletes only *.pyc files. Python will recompile on next run.",
            ),
            agent_permissions={"shell:read", "shell:write", "file:delete"},
            required_permissions={"shell:write", "file:delete"},
        )

        assert result.authorized is True
        assert result.threshold == "unanimous"
        assert result.risk_level == "high"

    @pytest.mark.asyncio
    async def test_read_only_claim_with_redirection_forces_unanimous(self):
        """Read-only requests that write to disk via redirection get a unanimous floor."""
        chairman = _make_chairman(threshold="quorum", risk_level="low")

        result = await chairman.pre_authorize(
            intent=_make_intent(
                "/usr/bin/grep -r 'ERROR' /app/logs/ > /tmp/error-summary.txt"
                " && /bin/cat /tmp/error-summary.txt",
                action_type="file.read",
                description="Search logs for errors",
                blast_radius="Read-only log search. No application logs modified.",
                target_environment="production",
            ),
            agent_permissions={"shell:read", "shell:write", "file:read"},
            required_permissions={"shell:read"},
        )

        assert result.authorized is True
        assert result.threshold == "unanimous"
        assert result.risk_level == "high"

    @pytest.mark.asyncio
    async def test_read_only_claim_with_stderr_dev_null_redirection_allows_quorum(self):
        """Stderr suppression to /dev/null is not treated as a disk write."""
        chairman = _make_chairman(threshold="quorum", risk_level="low", allow_quorum=True)

        result = await chairman.pre_authorize(
            intent=_make_intent(
                "/usr/bin/grep -rn MissingSessionEntryReservedSlotKeys"
                " /workspace/projects/openclaw/src/ 2>/dev/null | /usr/bin/head -20",
                action_type="file.read",
                description="Search source files for a symbol definition and usages",
                blast_radius="Read-only source inspection. No files modified.",
                target_environment="development",
            ),
            agent_permissions={"shell:read", "file:read"},
            required_permissions={"shell:read"},
        )

        assert result.authorized is True
        assert result.threshold == "quorum"
        assert result.risk_level == "low"

    @pytest.mark.asyncio
    async def test_direct_council_key_directory_read_forces_unanimous(self):
        """Direct reads from the council key directory require unanimous review."""
        chairman = _make_chairman(threshold="quorum", risk_level="low")

        result = await chairman.pre_authorize(
            intent=_make_intent(
                "/bin/cat /app/keys/council.pub",
                action_type="file.read",
                description="Read council public key",
                blast_radius="Read-only. No files modified.",
            ),
            agent_permissions={"shell:read", "file:read"},
            required_permissions={"shell:read"},
        )

        assert result.authorized is True
        assert result.threshold == "unanimous"
        assert result.risk_level == "high"

    @pytest.mark.asyncio
    async def test_double_slash_council_key_directory_read_forces_unanimous(self):
        chairman = _make_chairman(threshold="quorum", risk_level="low")

        result = await chairman.pre_authorize(
            intent=_make_intent(
                "/bin/cat //app/keys/council.key",
                action_type="file.read",
                description="Read council key",
                blast_radius="Read-only. No files modified.",
            ),
            agent_permissions={"shell:read", "file:read"},
            required_permissions={"shell:read"},
        )

        assert result.authorized is True
        assert result.threshold == "unanimous"
        assert result.risk_level == "high"

    @pytest.mark.asyncio
    async def test_glob_council_key_directory_read_forces_unanimous(self):
        chairman = _make_chairman(threshold="quorum", risk_level="low")

        result = await chairman.pre_authorize(
            intent=_make_intent(
                "/bin/cat /app/ke*/council.key",
                action_type="file.read",
                description="Read council key",
                blast_radius="Read-only. No files modified.",
            ),
            agent_permissions={"shell:read", "file:read"},
            required_permissions={"shell:read"},
        )

        assert result.authorized is True
        assert result.threshold == "unanimous"
        assert result.risk_level == "high"

    @pytest.mark.asyncio
    async def test_normalized_council_key_directory_read_forces_unanimous(self):
        chairman = _make_chairman(threshold="quorum", risk_level="low")

        result = await chairman.pre_authorize(
            intent=_make_intent(
                "/bin/cat /app//keys/council.pub",
                action_type="file.read",
                description="Read council public key",
                blast_radius="Read-only. No files modified.",
            ),
            agent_permissions={"shell:read", "file:read"},
            required_permissions={"shell:read"},
        )

        assert result.authorized is True
        assert result.threshold == "unanimous"
        assert result.risk_level == "high"

    @pytest.mark.asyncio
    async def test_admin_destructive_command_authorized(self):
        """Admin agent requesting destructive command proceeds to LLM."""
        chairman = _make_chairman(threshold="unanimous", risk_level="high")

        result = await chairman.pre_authorize(
            intent=_make_intent("rm -rf /var/data"),
            agent_permissions={
                "shell:read",
                "shell:write",
                "file:read",
                "file:write",
                "file:delete",
                "infra:deploy",
                "infra:destroy",
            },
            required_permissions={"shell:write", "file:delete"},
        )

        assert result.authorized is True
        assert result.threshold == "unanimous"
        chairman._backend.complete.assert_called_once()

    @pytest.mark.asyncio
    async def test_readonly_agent_destructive_denied(self):
        """Read-only agent requesting destructive command denied at gate."""
        chairman = _make_chairman()

        result = await chairman.pre_authorize(
            intent=_make_intent("rm -rf /var/data"),
            agent_permissions={"shell:read", "file:read"},
            required_permissions={"shell:write", "file:delete"},
        )

        assert result.authorized is False
        assert "shell:write" in result.missing_permissions
        assert "file:delete" in result.missing_permissions
        chairman._backend.complete.assert_not_called()


class TestChairmanPreAuthDecisionModel:
    def test_authorized_with_threshold(self):
        d = ChairmanPreAuthDecision(
            authorized=True, threshold="quorum", risk_level="low", reasoning="ok"
        )
        assert d.authorized is True
        assert d.threshold == "quorum"

    def test_unauthorized_none_threshold(self):
        d = ChairmanPreAuthDecision(
            authorized=False,
            threshold=None,
            reasoning="denied",
            missing_permissions=["shell:write"],
        )
        assert d.authorized is False
        assert d.threshold is None
        assert d.missing_permissions == ["shell:write"]

    def test_risk_level_normalization(self):
        d = ChairmanPreAuthDecision(authorized=True, threshold="quorum", risk_level="UNKNOWN")
        assert d.risk_level == "medium"

    def test_threshold_normalization(self):
        d = ChairmanPreAuthDecision(authorized=True, threshold="INVALID")
        assert d.threshold == "unanimous"
