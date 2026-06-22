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
Intent types: what agents propose, not what they execute.

Intents are structured requests that describe desired outcomes,
justification, scope, and rollback plans. Uses Pydantic for validation.
"""

import re
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from .packaged_file import PackagedFile

INTENT_DESCRIPTION_MAX_LENGTH = 16000
INTENT_CONTEXT_MAX_LENGTH = 32000
INTENT_JUSTIFICATION_MAX_LENGTH = 32000
INTENT_BLAST_RADIUS_MAX_LENGTH = 16000


class IntentStatus(StrEnum):
    """Lifecycle states of an intent."""

    PENDING = "pending"
    EVALUATING = "evaluating"
    APPROVED = "approved"
    DENIED = "denied"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    EXPIRED = "expired"


class RollbackStrategy(StrEnum):
    """Available rollback strategies."""

    NONE = "none"
    AUTOMATIC = "automatic"
    MANUAL = "manual"
    SNAPSHOT = "snapshot"
    BACKUP_RESTORE = "backup_restore"


class CommandType(StrEnum):
    """Types of commands that can be executed."""

    SHELL = "shell"
    API = "api"
    SQL = "sql"
    PYTHON = "python"
    KUBECTL = "kubectl"
    SCRIPT = "script"


class TargetEnvironment(StrEnum):
    """Environment tiers for execution."""

    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
    DR = "dr"


class RollbackPlan(BaseModel):
    """
    Describes how to undo an action if needed.

    Every risky intent should have a rollback plan.
    """

    strategy: RollbackStrategy = RollbackStrategy.NONE
    steps: list[str] = Field(default_factory=list)
    estimated_duration_seconds: int = Field(default=0, ge=0)
    requires_manual_intervention: bool = False
    artifacts: dict[str, str] = Field(default_factory=dict)

    @field_validator("steps")
    @classmethod
    def validate_steps(cls, v: list[str], info) -> list[str]:
        if info.data.get("strategy") == RollbackStrategy.AUTOMATIC and not v:
            raise ValueError("Automatic rollback requires at least one step")
        return v

    def to_dict(self) -> dict:
        """
        Convert rollback plan to a dictionary.

        Returns
        -------
        dict
            Dictionary representation of the rollback plan.
        """
        return self.model_dump()

    @classmethod
    def from_dict(cls, data: dict) -> "RollbackPlan":
        """
        Create rollback plan from a dictionary.

        Parameters
        ----------
        data : dict
            Dictionary containing rollback plan data.

        Returns
        -------
        RollbackPlan
            The parsed rollback plan object.
        """
        return cls.model_validate(data)

    @classmethod
    def none(cls) -> "RollbackPlan":
        """
        Create a rollback plan indicating no rollback is possible.

        Returns
        -------
        RollbackPlan
            A rollback plan with strategy NONE.
        """
        return cls(strategy=RollbackStrategy.NONE, requires_manual_intervention=True)

    @classmethod
    def automatic(cls, steps: list[str]) -> "RollbackPlan":
        """
        Create an automatic rollback plan with defined steps.

        Parameters
        ----------
        steps : List[str]
            List of steps to execute for automatic rollback.

        Returns
        -------
        RollbackPlan
            A rollback plan with strategy AUTOMATIC and the provided steps.
        """
        return cls(strategy=RollbackStrategy.AUTOMATIC, steps=steps)


class Intent(BaseModel):
    """
    A structured request from an agent for a privileged action.

    This is what agents submit—not raw actions, but described intent.
    Council agents see the exact command that will execute.
    """

    model_config = {"use_enum_values": True}

    intent_id: str = Field(..., min_length=1, description="Unique identifier for this intent")
    agent_id: str = Field(
        ..., min_length=1, pattern=r"^[a-zA-Z0-9_-]+$", description="ID of the requesting agent"
    )
    council_id: str = Field(
        default="",
        pattern=r"^[a-zA-Z0-9_-]*$",
        description="ID of the council selected to review this intent",
    )
    # Internal only — no longer supplied by agents in NAT inputs. Kept on the
    # stored Intent for legacy call sites (ledger entries, pattern-matchers
    # that fall back when there's no command). The Chairman will eventually
    # derive required permissions via RBAC lookup instead of reading this.
    action_type: str = Field(
        default="system.exec",
        min_length=1,
        pattern=r"^[a-z]+\.[a-z_]+$",
        description=(
            "Action type in format 'category.action' "
            "(derived server-side; no longer agent-supplied)"
        ),
    )
    description: str = Field(
        ...,
        min_length=10,
        max_length=INTENT_DESCRIPTION_MAX_LENGTH,
        description="Human-readable description of the intent",
    )
    target_resource: str = Field(..., min_length=1, description="Resource being acted upon")

    command: str = Field(default="", description="Exact command to execute")
    command_type: CommandType = Field(default=CommandType.SHELL, description="Type of command")
    working_directory: str = Field(default="", description="Directory where command runs")

    target_host: str = Field(default="", description="Target host machine")
    target_environment: TargetEnvironment | None = Field(
        default=None, description="Environment tier"
    )
    target_cluster: str = Field(default="", description="Kubernetes cluster name")
    target_namespace: str = Field(default="", description="Kubernetes namespace")
    target_container: str = Field(default="", description="Container name")
    execution_context: dict[str, Any] = Field(
        default_factory=dict, description="Additional execution context"
    )

    context: str = Field(
        default="",
        max_length=INTENT_CONTEXT_MAX_LENGTH,
        description="Explanation of what agent was doing",
    )
    context_files: dict[str, str] = Field(
        default_factory=dict, description="Relevant file contents"
    )
    preceding_commands: list[str] = Field(
        default_factory=list, max_length=50, description="Commands already executed"
    )
    system_state: dict[str, Any] = Field(default_factory=dict, description="Current system state")

    parameters: dict[str, Any] = Field(default_factory=dict, description="Action parameters")
    # Sized to hold inline evidence blocks (kubectl get output, cosign verify
    # output, staging soak logs) that the council's own priorities require
    # for production state-changing work. Small prose caps were too tight to carry
    # the evidence the review criteria demand.
    justification: str = Field(
        default="",
        max_length=INTENT_JUSTIFICATION_MAX_LENGTH,
        description="Why this action is needed",
    )
    blast_radius: str = Field(
        default="",
        max_length=INTENT_BLAST_RADIUS_MAX_LENGTH,
        description="Scope of potential impact",
    )
    evidence: list[str] = Field(
        default_factory=list, max_length=20, description="Supporting evidence IDs"
    )
    certified_evidence_rendered: list[str] = Field(
        default_factory=list,
        description=(
            "Rendered executor-certified evidence blocks. Populated ONLY by the council's "
            "evidence-verification path after signature/key/hash checks pass — never settable "
            "from agent-supplied input. Kept separate from `evidence` so a voter's "
            "certified-vs-agent-asserted trust label comes from provenance, not string content."
        ),
    )
    packaged_files: list[PackagedFile] = Field(
        default_factory=list,
        max_length=5,
        description="Files packaged with the intent for code review and writ execution",
    )
    rollback_plan: RollbackPlan | None = Field(default=None, description="How to undo the action")
    requested_at: datetime | None = Field(default=None, description="When intent was created")
    expires_at: datetime | None = Field(default=None, description="When intent expires")
    status: IntentStatus = Field(default=IntentStatus.PENDING, description="Current status")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")

    @field_validator("intent_id")
    @classmethod
    def validate_intent_id(cls, v: str) -> str:
        if not v.startswith("intent-"):
            raise ValueError("intent_id must start with 'intent-'")
        return v

    @field_validator("command")
    @classmethod
    def validate_command(cls, v: str) -> str:
        dangerous_patterns = [
            r"rm\s+-rf\s+/\s*$",
            r":\(\)\s*{\s*:\|:\s*&\s*}\s*;",
            r">\s*/dev/sd[a-z]",
            r"mkfs\.",
            r"dd\s+if=.*of=/dev/sd",
        ]
        for pattern in dangerous_patterns:
            if re.search(pattern, v):
                raise ValueError(f"Command contains dangerous pattern: {pattern}")
        return v

    @field_validator("target_namespace")
    @classmethod
    def validate_namespace(cls, v: str) -> str:
        if v and not re.match(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", v):
            raise ValueError("Invalid Kubernetes namespace format")
        return v

    @model_validator(mode="after")
    def validate_execution_target(self) -> "Intent":
        has_host = bool(self.target_host)
        has_cluster = bool(self.target_cluster)
        if has_host and has_cluster:
            raise ValueError("Cannot specify both target_host and target_cluster")
        if self.target_namespace and not self.target_cluster:
            raise ValueError("target_namespace requires target_cluster")
        if self.target_container and not self.target_namespace:
            raise ValueError("target_container requires target_namespace")
        return self

    @model_validator(mode="after")
    def validate_production_requirements(self) -> "Intent":
        if self.target_environment == TargetEnvironment.PRODUCTION:
            if not self.justification:
                raise ValueError("Production actions require justification")
            if not self.blast_radius:
                raise ValueError("Production actions require blast_radius")
        return self

    @classmethod
    def create(
        cls,
        agent_id: str,
        description: str,
        target_resource: str,
        action_type: str | None = None,
        **kwargs,
    ) -> "Intent":
        """Factory method to create a new intent with generated ID.

        action_type is optional — agents no longer supply it, and the
        model field has a sensible default. Accepted here for
        backwards-compatibility with any caller that still passes it.
        """
        if action_type is not None:
            kwargs["action_type"] = action_type
        return cls(
            intent_id=f"intent-{uuid.uuid4().hex[:16]}",
            agent_id=agent_id,
            description=description,
            target_resource=target_resource,
            **kwargs,
        )

    def to_dict(self) -> dict:
        data = self.model_dump()
        if self.requested_at:
            data["requested_at"] = self.requested_at.isoformat()
        if self.expires_at:
            data["expires_at"] = self.expires_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Intent":
        return cls.model_validate(data)

    def content_for_hashing(self) -> dict:
        """Return canonical content for hash computation."""
        return {
            "intent_id": self.intent_id,
            "agent_id": self.agent_id,
            "council_id": self.council_id,
            "action_type": self.action_type,
            "description": self.description,
            "target_resource": self.target_resource,
            "command": self.command,
            "command_type": self.command_type.value
            if isinstance(self.command_type, CommandType)
            else self.command_type,
            "working_directory": self.working_directory,
            "target_host": self.target_host,
            "target_environment": self.target_environment.value
            if self.target_environment
            else None,
            "target_cluster": self.target_cluster,
            "target_namespace": self.target_namespace,
            "target_container": self.target_container,
            "execution_context": self.execution_context,
            "context": self.context,
            "context_files": self.context_files,
            "preceding_commands": self.preceding_commands,
            "system_state": self.system_state,
            "parameters": self.parameters,
            "justification": self.justification,
            "blast_radius": self.blast_radius,
            "evidence": self.evidence,
            # certified_evidence_rendered is intentionally excluded: it is derived,
            # verification-path-only presentation text, not part of the intent's identity.
            "packaged_files": [f.content_for_hashing() for f in self.packaged_files],
            "rollback_plan": self.rollback_plan.to_dict() if self.rollback_plan else None,
        }
