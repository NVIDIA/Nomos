# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for core types.

Following Lakos: test data types independently of business logic.
"""

import base64
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from nomos.level_0.types import (
    CapabilityScope,
    CapabilityStatus,
    CapabilityToken,
    Intent,
    PackagedFile,
    RollbackPlan,
)
from nomos.level_0.types.intent import (
    INTENT_BLAST_RADIUS_MAX_LENGTH,
    INTENT_CONTEXT_MAX_LENGTH,
    INTENT_DESCRIPTION_MAX_LENGTH,
    INTENT_JUSTIFICATION_MAX_LENGTH,
    CommandType,
    RollbackStrategy,
    TargetEnvironment,
)


class TestRollbackPlan:
    """Tests for RollbackPlan."""

    def test_none_strategy(self):
        plan = RollbackPlan.none()
        assert plan.strategy == "none"
        assert plan.requires_manual_intervention is True

    def test_automatic_strategy(self):
        steps = ["step1", "step2"]
        plan = RollbackPlan.automatic(steps)
        assert plan.strategy == "automatic"
        assert plan.steps == steps

    def test_to_dict_roundtrip(self):
        plan = RollbackPlan(
            strategy="automatic",
            steps=["undo1", "undo2"],
            estimated_duration_seconds=60,
        )
        data = plan.to_dict()
        restored = RollbackPlan.from_dict(data)
        assert restored.strategy == plan.strategy
        assert restored.steps == plan.steps


class TestIntent:
    """Tests for Intent."""

    def test_create_generates_id(self):
        intent = Intent.create(
            agent_id="agent-1",
            action_type="file.delete",
            description="Delete temp file",
            target_resource="/tmp/test.txt",
        )
        assert intent.intent_id.startswith("intent-")
        assert intent.agent_id == "agent-1"

    def test_to_dict_roundtrip(self):
        intent = Intent.create(
            agent_id="agent-1",
            action_type="file.delete",
            description="Delete temp file",
            target_resource="/tmp/test.txt",
            justification="Cleanup",
            rollback_plan=RollbackPlan.automatic(["restore from backup"]),
        )
        data = intent.to_dict()
        restored = Intent.from_dict(data)
        assert restored.intent_id == intent.intent_id
        assert restored.action_type == intent.action_type
        assert restored.rollback_plan.strategy == "automatic"

    def test_content_for_hashing_excludes_mutable_fields(self):
        intent = Intent.create(
            agent_id="agent-1",
            action_type="file.delete",
            description="Test hashing content for intent",
            target_resource="/test",
        )
        content = intent.content_for_hashing()
        assert "status" not in content
        assert "requested_at" not in content

    def test_certified_evidence_rendered_excluded_from_hash(self):
        # The certified-evidence carrier is derived presentation text and must not
        # be part of the intent's identity/hash surface (raw evidence still is).
        base = dict(
            agent_id="agent-1",
            action_type="file.delete",
            description="Hashing must ignore certified_evidence_rendered",
            target_resource="/test",
            evidence=["agent claim"],
        )
        without = Intent.create(**base)
        with_certified = Intent.create(
            **base,
            certified_evidence_rendered=["CERTIFIED EXECUTOR EVIDENCE ...verified..."],
        )
        content = with_certified.content_for_hashing()
        assert "certified_evidence_rendered" not in content
        assert content["evidence"] == ["agent claim"]
        # Same intent_id so the only difference is the carrier; hashes must match.
        assert (
            with_certified.model_copy(update={"intent_id": without.intent_id}).content_for_hashing()
            == without.content_for_hashing()
        )

    def test_certified_evidence_rendered_roundtrip_and_legacy_default(self):
        intent = Intent.create(
            agent_id="agent-1",
            description="Round-trip the certified evidence carrier",
            target_resource="/test",
            evidence=["agent claim"],
            certified_evidence_rendered=["CERTIFIED EXECUTOR EVIDENCE ...verified..."],
        )
        restored = Intent.from_dict(intent.to_dict())
        assert restored.certified_evidence_rendered == intent.certified_evidence_rendered
        assert restored.evidence == ["agent claim"]

        # Legacy records persisted before this field existed must still load,
        # defaulting the carrier to empty (never silently certified).
        legacy = intent.to_dict()
        legacy.pop("certified_evidence_rendered")
        assert Intent.from_dict(legacy).certified_evidence_rendered == []


class TestPackagedFile:
    def test_hash_computed_from_base64_content(self):
        content = base64.b64encode(b"print('hello')\n").decode()
        packaged_file = PackagedFile(path="scripts/task.py", content_base64=content)

        assert packaged_file.sha256
        assert packaged_file.decoded_bytes() == b"print('hello')\n"
        assert packaged_file.path == "scripts/task.py"

    def test_rejects_hash_mismatch(self):
        content = base64.b64encode(b"safe").decode()
        with pytest.raises(ValidationError, match="sha256"):
            PackagedFile(path="script.sh", content_base64=content, sha256="00" * 32)

    def test_rejects_path_escape(self):
        content = base64.b64encode(b"safe").decode()
        with pytest.raises(ValidationError, match="relative"):
            PackagedFile(path="../escape.sh", content_base64=content)


class TestCapabilityScope:
    """Tests for CapabilityScope."""

    def test_matches_exact(self):
        scope = CapabilityScope(
            action_type="file.delete",
            resource_pattern="/tmp/*",
            allowed_operations=frozenset(["execute"]),
        )
        assert scope.matches("file.delete", "/tmp/test.txt", "execute") is True
        assert scope.matches("file.read", "/tmp/test.txt", "execute") is False

    def test_matches_wildcard_action(self):
        scope = CapabilityScope(
            action_type="*",
            resource_pattern="/tmp/*",
        )
        assert scope.matches("any.action", "/tmp/test.txt", "execute") is True

    def test_matches_resource_pattern(self):
        scope = CapabilityScope(
            action_type="file.delete",
            resource_pattern="/var/log/*.log",
        )
        assert scope.matches("file.delete", "/var/log/app.log", "execute") is True
        assert scope.matches("file.delete", "/var/log/app.txt", "execute") is False


class TestCapabilityToken:
    """Tests for CapabilityToken."""

    def test_create_generates_id(self):
        scope = CapabilityScope(action_type="test", resource_pattern="*")
        now = datetime.now(UTC)
        token = CapabilityToken.create(
            intent_id="intent-123",
            scope=scope,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
            ledger_anchor="hash:abc",
            issuer_id="council",
            bearer_agent_id="agent-1",
        )
        assert token.token_id.startswith("cap-")

    def test_is_valid(self):
        scope = CapabilityScope(action_type="test", resource_pattern="*")
        now = datetime.now(UTC)
        token = CapabilityToken.create(
            intent_id="intent-123",
            scope=scope,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
            ledger_anchor="hash:abc",
            issuer_id="council",
            bearer_agent_id="agent-1",
        )
        assert token.is_valid(now) is True
        assert token.is_valid(now + timedelta(minutes=10)) is False

    def test_record_invocation(self):
        scope = CapabilityScope(action_type="test", resource_pattern="*", max_invocations=2)
        now = datetime.now(UTC)
        token = CapabilityToken.create(
            intent_id="intent-123",
            scope=scope,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
            ledger_anchor="hash:abc",
            issuer_id="council",
            bearer_agent_id="agent-1",
        )

        token.record_invocation()
        assert token.invocation_count == 1
        assert token.status == CapabilityStatus.ACTIVE

        token.record_invocation()
        assert token.invocation_count == 2
        assert token.status == CapabilityStatus.USED


class TestIntentValidation:
    """Tests for Intent Pydantic validation."""

    def test_valid_intent_creation(self):
        """Test that a valid intent is created successfully."""
        intent = Intent.create(
            agent_id="agent-1",
            action_type="file.delete",
            description="Delete temporary files from the system",
            target_resource="/tmp/test",
            command="rm -rf /tmp/test/*",
            command_type=CommandType.SHELL,
            working_directory="/tmp",
            justification="Cleanup old files",
        )
        assert intent.intent_id.startswith("intent-")
        assert intent.command_type == CommandType.SHELL

    def test_invalid_intent_id_prefix(self):
        """Test that intent_id must start with 'intent-'."""
        with pytest.raises(ValidationError) as exc:
            Intent(
                intent_id="bad-id",
                agent_id="agent-1",
                action_type="file.delete",
                description="Delete temporary files",
                target_resource="/tmp/test",
            )
        assert "intent_id must start with 'intent-'" in str(exc.value)

    def test_invalid_agent_id_format(self):
        """Test that agent_id must match pattern."""
        with pytest.raises(ValidationError) as exc:
            Intent.create(
                agent_id="agent@invalid!",
                action_type="file.delete",
                description="Delete temporary files",
                target_resource="/tmp/test",
            )
        assert "agent_id" in str(exc.value)

    def test_invalid_action_type_format(self):
        """Test that action_type must be in category.action format."""
        with pytest.raises(ValidationError) as exc:
            Intent.create(
                agent_id="agent-1",
                action_type="invalid",
                description="Delete temporary files",
                target_resource="/tmp/test",
            )
        assert "action_type" in str(exc.value)

    def test_description_too_short(self):
        """Test that description must be at least 10 characters."""
        with pytest.raises(ValidationError) as exc:
            Intent.create(
                agent_id="agent-1",
                action_type="file.delete",
                description="Short",
                target_resource="/tmp/test",
            )
        assert "description" in str(exc.value)

    def test_rich_intent_text_fields_accept_large_inputs(self):
        """Long evidence-heavy prose should fit in the structured intent."""
        intent = Intent.create(
            agent_id="agent-1",
            action_type="deploy.application",
            description="D" * 2000,
            target_resource="/apps/myapp",
            context="C" * 6000,
            justification="J" * 12000,
            blast_radius="B" * 4000,
        )

        assert len(intent.description) == 2000
        assert len(intent.context) == 6000
        assert len(intent.justification) == 12000
        assert len(intent.blast_radius) == 4000

    def test_rich_intent_text_fields_still_have_upper_bounds(self):
        with pytest.raises(ValidationError):
            Intent.create(
                agent_id="agent-1",
                action_type="deploy.application",
                description="D" * (INTENT_DESCRIPTION_MAX_LENGTH + 1),
                target_resource="/apps/myapp",
            )

        with pytest.raises(ValidationError):
            Intent.create(
                agent_id="agent-1",
                action_type="deploy.application",
                description="Deploy application with oversized context",
                target_resource="/apps/myapp",
                context="C" * (INTENT_CONTEXT_MAX_LENGTH + 1),
            )

        with pytest.raises(ValidationError):
            Intent.create(
                agent_id="agent-1",
                action_type="deploy.application",
                description="Deploy application with oversized justification",
                target_resource="/apps/myapp",
                justification="J" * (INTENT_JUSTIFICATION_MAX_LENGTH + 1),
            )

        with pytest.raises(ValidationError):
            Intent.create(
                agent_id="agent-1",
                action_type="deploy.application",
                description="Deploy application with oversized blast radius",
                target_resource="/apps/myapp",
                blast_radius="B" * (INTENT_BLAST_RADIUS_MAX_LENGTH + 1),
            )

    def test_dangerous_command_rejected(self):
        """Test that dangerous commands are rejected."""
        with pytest.raises(ValidationError) as exc:
            Intent.create(
                agent_id="agent-1",
                action_type="file.delete",
                description="Delete everything from root",
                target_resource="/",
                command="rm -rf / ",
            )
        assert "dangerous pattern" in str(exc.value)

    def test_invalid_namespace_format(self):
        """Test that Kubernetes namespace must be valid format."""
        with pytest.raises(ValidationError) as exc:
            Intent.create(
                agent_id="agent-1",
                action_type="deploy.application",
                description="Deploy application to cluster",
                target_resource="/apps/myapp",
                target_cluster="prod-cluster",
                target_namespace="Invalid_Namespace",
            )
        assert "namespace" in str(exc.value).lower()

    def test_namespace_requires_cluster(self):
        """Test that target_namespace requires target_cluster."""
        with pytest.raises(ValidationError) as exc:
            Intent.create(
                agent_id="agent-1",
                action_type="deploy.application",
                description="Deploy application to cluster",
                target_resource="/apps/myapp",
                target_namespace="my-namespace",
            )
        assert "target_namespace requires target_cluster" in str(exc.value)

    def test_container_requires_namespace(self):
        """Test that target_container requires target_namespace."""
        with pytest.raises(ValidationError) as exc:
            Intent.create(
                agent_id="agent-1",
                action_type="deploy.application",
                description="Deploy application to cluster",
                target_resource="/apps/myapp",
                target_cluster="prod-cluster",
                target_container="my-container",
            )
        assert "target_container requires target_namespace" in str(exc.value)

    def test_cannot_specify_host_and_cluster(self):
        """Test that target_host and target_cluster are mutually exclusive."""
        with pytest.raises(ValidationError) as exc:
            Intent.create(
                agent_id="agent-1",
                action_type="deploy.application",
                description="Deploy application to server",
                target_resource="/apps/myapp",
                target_host="prod-server-01",
                target_cluster="prod-cluster",
            )
        assert "Cannot specify both target_host and target_cluster" in str(exc.value)

    def test_production_requires_justification(self):
        """Test that production environment requires justification."""
        with pytest.raises(ValidationError) as exc:
            Intent.create(
                agent_id="agent-1",
                action_type="deploy.application",
                description="Deploy application to production",
                target_resource="/apps/myapp",
                target_environment=TargetEnvironment.PRODUCTION,
            )
        assert "Production actions require justification" in str(exc.value)

    def test_production_requires_blast_radius(self):
        """Test that production environment requires blast_radius."""
        with pytest.raises(ValidationError) as exc:
            Intent.create(
                agent_id="agent-1",
                action_type="deploy.application",
                description="Deploy application to production",
                target_resource="/apps/myapp",
                target_environment=TargetEnvironment.PRODUCTION,
                justification="Critical deployment",
            )
        assert "Production actions require blast_radius" in str(exc.value)

    def test_valid_production_intent(self):
        """Test that production intent with all required fields succeeds."""
        intent = Intent.create(
            agent_id="agent-1",
            action_type="deploy.application",
            description="Deploy application to production",
            target_resource="/apps/myapp",
            target_environment=TargetEnvironment.PRODUCTION,
            justification="Critical deployment for customer fix",
            blast_radius="Single application, no database changes",
        )
        assert intent.target_environment == TargetEnvironment.PRODUCTION

    def test_valid_kubernetes_intent(self):
        """Test that valid Kubernetes intent succeeds."""
        intent = Intent.create(
            agent_id="agent-1",
            action_type="deploy.application",
            description="Deploy application to Kubernetes cluster",
            target_resource="/apps/myapp",
            target_cluster="prod-us-east-1",
            target_namespace="default",
            target_container="app-container",
        )
        assert intent.target_cluster == "prod-us-east-1"
        assert intent.target_namespace == "default"
        assert intent.target_container == "app-container"

    def test_command_type_enum(self):
        """Test that command_type uses enum values."""
        intent = Intent.create(
            agent_id="agent-1",
            action_type="database.query",
            description="Query database for user records",
            target_resource="postgres://prod-db/users",
            command="SELECT * FROM users WHERE active = true",
            command_type=CommandType.SQL,
        )
        assert intent.command_type == CommandType.SQL

    def test_context_fields(self):
        """Test that context fields are properly set."""
        intent = Intent.create(
            agent_id="agent-1",
            action_type="file.delete",
            description="Delete old log files from disk",
            target_resource="/var/log/app",
            context="Disk at 95%, need to free space",
            context_files={"/etc/logrotate.conf": "contents here"},
            preceding_commands=["df -h", "du -sh /var/log/*"],
            system_state={"disk_usage_percent": 95},
        )
        assert intent.context == "Disk at 95%, need to free space"
        assert len(intent.preceding_commands) == 2
        assert intent.system_state["disk_usage_percent"] == 95

    def test_serialization_roundtrip(self):
        """Test that Intent serializes and deserializes correctly."""
        original = Intent.create(
            agent_id="agent-1",
            action_type="file.delete",
            description="Delete old log files from disk",
            target_resource="/var/log/app",
            command="find /var/log -name '*.log' -mtime +30 -delete",
            command_type=CommandType.SHELL,
            target_host="prod-server-01",
            target_environment=TargetEnvironment.PRODUCTION,
            context="Disk cleanup operation",
            justification="Disk at 95%",
            blast_radius="Log files only",
            rollback_plan=RollbackPlan.none(),
        )
        data = original.to_dict()
        restored = Intent.from_dict(data)

        assert restored.intent_id == original.intent_id
        assert restored.command == original.command
        assert restored.target_host == original.target_host
        assert restored.rollback_plan.strategy == RollbackStrategy.NONE


class TestRollbackPlanValidation:
    """Tests for RollbackPlan Pydantic validation."""

    def test_automatic_requires_steps(self):
        """Test that automatic rollback requires steps."""
        with pytest.raises(ValidationError) as exc:
            RollbackPlan(strategy=RollbackStrategy.AUTOMATIC, steps=[])
        assert "Automatic rollback requires at least one step" in str(exc.value)

    def test_automatic_with_steps_succeeds(self):
        """Test that automatic rollback with steps succeeds."""
        plan = RollbackPlan.automatic(["step 1", "step 2"])
        assert plan.strategy == RollbackStrategy.AUTOMATIC
        assert len(plan.steps) == 2

    def test_negative_duration_rejected(self):
        """Test that negative duration is rejected."""
        with pytest.raises(ValidationError):
            RollbackPlan(
                strategy=RollbackStrategy.MANUAL,
                estimated_duration_seconds=-1,
            )

    def test_serialization_roundtrip(self):
        """Test that RollbackPlan serializes and deserializes correctly."""
        original = RollbackPlan(
            strategy=RollbackStrategy.SNAPSHOT,
            steps=["create snapshot", "verify snapshot"],
            estimated_duration_seconds=120,
            requires_manual_intervention=True,
            artifacts={"snapshot_id": "snap-123"},
        )
        data = original.to_dict()
        restored = RollbackPlan.from_dict(data)

        assert restored.strategy == RollbackStrategy.SNAPSHOT
        assert restored.estimated_duration_seconds == 120
        assert restored.artifacts["snapshot_id"] == "snap-123"
