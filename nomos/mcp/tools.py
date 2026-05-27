"""
MCP tool definitions for the Security Council.

These are the tools exposed to agents via MCP protocol.
"""

from dataclasses import dataclass
from typing import Any

from nomos.level_0.types import Intent, RollbackPlan
from nomos.level_4.council import SecurityCouncil


@dataclass
class ToolResult:
    """Standard result format for MCP tools."""

    success: bool
    data: dict[str, Any]
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "data": self.data,
            "error": self.error,
        }


def propose_intent(
    council: SecurityCouncil,
    agent_id: str,
    description: str,
    target_resource: str,
    justification: str,
    command: str = "",
    parameters: dict[str, Any] | None = None,
    blast_radius: str = "",
    evidence: list[str] | None = None,
    action_type: str = "",
    working_directory: str = "",
    target_host: str = "",
    target_environment: str = "",
    target_cluster: str = "",
    target_namespace: str = "",
    context: str = "",
    rollback_strategy: str = "none",
    rollback_steps: list[str] | None = None,
) -> ToolResult:
    """
    Propose an intent to the Security Council.

    This is the primary entry point for agents to request privileged actions.

    Args:
        council: The Security Council instance
        agent_id: ID of the requesting agent
        description: Human-readable description of what the agent wants to do
        target_resource: The resource to act upon
        justification: Why this action is needed
        command: Exact command to evaluate, if any
        parameters: Action-specific parameters
        blast_radius: Description of potential impact
        evidence: Supporting evidence for the request
        action_type: Legacy override for non-command callers
        working_directory: Directory where the command runs
        target_host: Target host machine
        target_environment: development, staging, production, or dr
        target_cluster: Kubernetes cluster name
        target_namespace: Kubernetes namespace
        context: What the agent was doing before this request
        rollback_strategy: "none", "automatic", or "manual"
        rollback_steps: Steps to rollback if automatic

    Returns:
        ToolResult with proposal_id and status
    """
    rollback_plan = None
    if rollback_strategy == "automatic" and rollback_steps:
        rollback_plan = RollbackPlan.automatic(rollback_steps)
    elif rollback_strategy == "none":
        rollback_plan = RollbackPlan.none()

    intent_kwargs: dict[str, Any] = {
        "agent_id": agent_id,
        "description": description,
        "target_resource": target_resource,
        "parameters": parameters or {},
        "justification": justification,
        "blast_radius": blast_radius,
        "evidence": evidence or [],
        "rollback_plan": rollback_plan,
        "context": context,
    }
    if command:
        intent_kwargs["command"] = command
    if action_type:
        intent_kwargs["action_type"] = action_type
    if working_directory:
        intent_kwargs["working_directory"] = working_directory
    if target_host:
        intent_kwargs["target_host"] = target_host
    if target_environment:
        intent_kwargs["target_environment"] = target_environment
    if target_cluster:
        intent_kwargs["target_cluster"] = target_cluster
    if target_namespace:
        intent_kwargs["target_namespace"] = target_namespace

    intent = Intent.create(**intent_kwargs)

    result = council.submit_intent(intent)

    if not result.success:
        return ToolResult(
            success=False,
            data={"intent_id": intent.intent_id},
            error=result.error,
        )

    workflow = result.workflow
    return ToolResult(
        success=True,
        data={
            "intent_id": intent.intent_id,
            "state": workflow.state.value if workflow else "unknown",
            "requires_approval": result.requires_approval,
            "evaluation": workflow.evaluation_result.to_dict()
            if workflow and workflow.evaluation_result
            else None,
        },
    )


def check_proposal_status(
    council: SecurityCouncil,
    intent_id: str,
) -> ToolResult:
    """
    Check the status of a previously submitted intent.

    Use this to poll for approval status or execution results.

    Args:
        council: The Security Council instance
        intent_id: ID of the intent to check

    Returns:
        ToolResult with current workflow state
    """
    workflow = council.get_workflow(intent_id)

    if workflow is None:
        return ToolResult(
            success=False,
            data={},
            error=f"Intent {intent_id} not found",
        )

    return ToolResult(
        success=True,
        data=workflow.summary(),
    )


def approve_intent(
    council: SecurityCouncil,
    intent_id: str,
    approver_id: str,
    reason: str = "",
) -> ToolResult:
    """
    Approve an intent that is awaiting approval.

    This casts an approval vote. Depending on quorum requirements,
    multiple approvals may be needed.

    Args:
        council: The Security Council instance
        intent_id: ID of the intent to approve
        approver_id: ID of the approving council member
        reason: Optional reason for approval

    Returns:
        ToolResult with updated workflow state
    """
    result = council.approve_intent(intent_id, approver_id, reason)

    if not result.success:
        return ToolResult(
            success=False,
            data={"intent_id": intent_id},
            error=result.error,
        )

    workflow = result.workflow
    return ToolResult(
        success=True,
        data={
            "intent_id": intent_id,
            "state": workflow.state.value if workflow else "unknown",
            "vote_record": workflow.vote_record.to_dict()
            if workflow and workflow.vote_record
            else None,
        },
    )


def deny_intent(
    council: SecurityCouncil,
    intent_id: str,
    denier_id: str,
    reason: str = "",
) -> ToolResult:
    """
    Deny an intent that is awaiting approval.

    Args:
        council: The Security Council instance
        intent_id: ID of the intent to deny
        denier_id: ID of the denying council member
        reason: Reason for denial

    Returns:
        ToolResult with updated workflow state
    """
    result = council.deny_intent(intent_id, denier_id, reason)

    if not result.success:
        return ToolResult(
            success=False,
            data={"intent_id": intent_id},
            error=result.error,
        )

    workflow = result.workflow
    return ToolResult(
        success=True,
        data={
            "intent_id": intent_id,
            "state": workflow.state.value if workflow else "unknown",
            "denial_reason": workflow.denial_reason if workflow else reason,
        },
    )


def execute_intent(
    council: SecurityCouncil,
    intent_id: str,
) -> ToolResult:
    """
    Execute an approved intent.

    This mints a capability token (if needed), executes the action,
    and verifies the result.

    Args:
        council: The Security Council instance
        intent_id: ID of the intent to execute

    Returns:
        ToolResult with execution outcome
    """
    result = council.execute_intent(intent_id)

    if not result.success:
        return ToolResult(
            success=False,
            data={"intent_id": intent_id},
            error=result.error,
        )

    workflow = result.workflow
    data = {
        "intent_id": intent_id,
        "state": workflow.state.value if workflow else "unknown",
        "succeeded": workflow.succeeded if workflow else False,
    }

    if workflow:
        if workflow.execution_result:
            data["execution"] = {  # type: ignore[assignment]
                "status": workflow.execution_result.status.value,
                "output": workflow.execution_result.output,
                "duration_seconds": workflow.execution_result.duration_seconds,
            }
        if workflow.verification_result:
            data["verification"] = {  # type: ignore[assignment]
                "status": workflow.verification_result.status.value,
                "all_passed": workflow.verification_result.all_passed,
            }

    return ToolResult(success=True, data=data)


def get_ledger_status(council: SecurityCouncil) -> ToolResult:
    """
    Get the current status of the permissioned action ledger.

    Returns chain integrity information for audit.

    Args:
        council: The Security Council instance

    Returns:
        ToolResult with ledger status
    """
    try:
        status = council.get_ledger_status()
        is_valid = council.validate_ledger()
        status["validated"] = is_valid
        return ToolResult(success=True, data=status)
    except Exception as e:
        return ToolResult(
            success=False,
            data={},
            error=f"Ledger validation failed: {e}",
        )


def get_pending_approvals(council: SecurityCouncil) -> ToolResult:
    """
    Get all intents awaiting approval.

    Args:
        council: The Security Council instance

    Returns:
        ToolResult with list of pending approvals
    """
    workflows = council.get_pending_approvals()
    return ToolResult(
        success=True,
        data={
            "count": len(workflows),
            "pending": [w.summary() for w in workflows],
        },
    )
