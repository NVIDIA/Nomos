"""
Security Council orchestration for Nomos privileged operations.

This is the main orchestrator that ties together all components.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from nomos.level_0.clock import Clock, SystemClock
from nomos.level_0.crypto import KeyPair, Signer
from nomos.level_0.types import (
    AgentRole,
    CapabilityScope,
    CapabilityToken,
    CouncilMember,
    Intent,
)
from nomos.level_1.ledger import LedgerChain, LedgerEntryType
from nomos.level_1.policy import DecisionType, PolicyEngine
from nomos.level_2.capability import CapabilityService, MintRequest
from nomos.level_2.quorum import QuorumManager, QuorumStatus, VoteType
from nomos.level_3.executor import Action, ActionHandler, SandboxExecutor
from nomos.level_3.verifier import VerificationStatus, Verifier

from .workflow import IntentWorkflow, WorkflowState


@dataclass
class CouncilConfig:
    """Configuration for the Security Council."""

    council_id: str = "council-default"
    default_token_ttl_seconds: int = 300
    auto_approve_low_risk: bool = False
    require_justification: bool = True
    max_pending_intents: int = 1000


@dataclass
class SubmitResult:
    """Result of submitting an intent."""

    success: bool
    workflow: IntentWorkflow | None = None
    error: str | None = None
    requires_approval: bool = False


@dataclass
class ExecuteResult:
    """Result of executing an intent."""

    success: bool
    workflow: IntentWorkflow | None = None
    error: str | None = None


class SecurityCouncil:
    """
    The Security Council: Nomos authorization for privileged operations.

    This is the central orchestrator that:
    1. Receives intent submissions from agents
    2. Evaluates intents against policy
    3. Manages quorum-based approval when needed
    4. Mints capability tokens for approved intents
    5. Orchestrates sandboxed execution
    6. Verifies execution and triggers rollback if needed
    7. Records everything in the permissioned ledger
    """

    def __init__(
        self,
        config: CouncilConfig | None = None,
        ledger: LedgerChain | None = None,
        policy_engine: PolicyEngine | None = None,
        capability_service: CapabilityService | None = None,
        quorum_manager: QuorumManager | None = None,
        executor: SandboxExecutor | None = None,
        verifier: Verifier | None = None,
        clock: Clock | None = None,
        signer: Signer | None = None,
    ):
        self._config = config or CouncilConfig()
        self._clock = clock or SystemClock()

        if signer is None:
            key_pair = KeyPair.generate(key_id=self._config.council_id)
            signer = Signer(key_pair)
        self._signer = signer

        self._ledger = ledger or LedgerChain(
            clock=self._clock,
            signer=self._signer,
            chain_id=f"ledger-{self._config.council_id}",
        )

        if self._ledger.length() == 0:
            self._ledger.initialize(
                creator_id=self._config.council_id,
                description="Nomos Security Council Ledger",
            )

        self._policy_engine = policy_engine or PolicyEngine()

        self._capability_service = capability_service or CapabilityService(
            ledger=self._ledger,
            clock=self._clock,
            signer=self._signer,
            issuer_id=self._config.council_id,
            default_ttl_seconds=self._config.default_token_ttl_seconds,
        )

        self._quorum_manager = quorum_manager or QuorumManager(
            ledger=self._ledger,
            clock=self._clock,
        )

        self._executor = executor or SandboxExecutor(
            capability_service=self._capability_service,
            ledger=self._ledger,
            clock=self._clock,
        )

        self._verifier = verifier or Verifier(
            ledger=self._ledger,
            clock=self._clock,
        )

        self._workflows: dict[str, IntentWorkflow] = {}
        self._agent_roles: dict[str, AgentRole] = {}

    def _coerce_expiry_datetime(self, value: Any) -> datetime | None:
        """Normalize supported expiry representations to a datetime."""
        if value is None:
            return None

        now = self._clock.now()
        expiry: datetime
        if isinstance(value, datetime):
            expiry = value
        elif isinstance(value, int | float):
            expiry = datetime.fromtimestamp(float(value), tz=now.tzinfo)
        elif isinstance(value, str):
            try:
                expiry = datetime.fromisoformat(value)
            except ValueError:
                try:
                    expiry = datetime.fromtimestamp(float(value), tz=now.tzinfo)
                except ValueError:
                    return None
        else:
            return None

        if expiry.tzinfo is None and now.tzinfo is not None:
            expiry = expiry.replace(tzinfo=now.tzinfo)
        return expiry

    def _workflow_expiry(self, workflow: IntentWorkflow) -> datetime | None:
        """Return the lazy expiry time for a decision-stage workflow."""
        metadata_expiry = self._coerce_expiry_datetime(workflow.metadata.get("decision_expires_at"))
        if metadata_expiry is not None:
            return metadata_expiry

        intent_expiry = self._coerce_expiry_datetime(workflow.intent.expires_at)
        if intent_expiry is not None:
            return intent_expiry

        if workflow.state == WorkflowState.APPROVED and workflow.approved_at is not None:
            return workflow.approved_at + timedelta(seconds=self._config.default_token_ttl_seconds)
        if workflow.state == WorkflowState.AWAITING_APPROVAL and workflow.submitted_at is not None:
            return workflow.submitted_at + timedelta(seconds=self._config.default_token_ttl_seconds)
        return None

    def _expire_stale_workflows(self) -> None:
        """Mark stale approval-stage workflows terminal before counting them."""
        now = self._clock.now()
        for workflow in self._workflows.values():
            if workflow.state not in (
                WorkflowState.AWAITING_APPROVAL,
                WorkflowState.APPROVED,
            ):
                continue

            expiry = self._workflow_expiry(workflow)
            if expiry is None or now < expiry:
                continue

            workflow.transition_to(WorkflowState.EXPIRED)
            workflow.completed_at = now
            workflow.error = "Workflow expired before execution"
            workflow.metadata["expired_at"] = now.isoformat()

    def _prune_terminal_workflows(self) -> None:
        """Evict the oldest terminal workflows once retained history exceeds the
        pending cap.

        Terminal workflows are kept so callers can still query the status of a
        recently finished intent, but a long-lived council would otherwise grow
        the workflow map without bound. Workflows are inserted in submission
        order, so dropping the earliest terminal entries evicts the oldest
        history first while preserving the most recent results.
        """
        self._expire_stale_workflows()
        terminal_ids = [
            intent_id for intent_id, workflow in self._workflows.items() if workflow.is_terminal
        ]
        overflow = len(terminal_ids) - self._config.max_pending_intents
        if overflow <= 0:
            return
        for intent_id in terminal_ids[:overflow]:
            self._workflows.pop(intent_id, None)

    def _resolve_required_weight(
        self,
        policy_required_weight: int = 0,
        review_threshold: str | None = None,
    ) -> int:
        """Resolve the final approval weight for a council-reviewed intent."""
        total_weight = self._quorum_manager.total_available_weight()

        threshold = (review_threshold or "unanimous").strip().lower()
        threshold_weight = 0
        if total_weight > 0:
            if threshold == "quorum":
                threshold_weight = max(1, (total_weight // 2) + 1)
            else:
                threshold_weight = total_weight

        if policy_required_weight > 0 and threshold_weight > 0:
            return max(policy_required_weight, threshold_weight)
        if threshold_weight > 0:
            return threshold_weight
        if policy_required_weight > 0:
            return policy_required_weight
        return 1

    def _apply_quorum_result(
        self,
        workflow: IntentWorkflow,
        status: QuorumStatus,
        denial_reason: str = "",
    ) -> None:
        """Apply the current quorum outcome to the workflow state."""
        if status == QuorumStatus.APPROVED and workflow.state == WorkflowState.AWAITING_APPROVAL:
            workflow.transition_to(WorkflowState.APPROVED)
            workflow.approved_at = self._clock.now()
            workflow.denial_reason = ""
        elif status == QuorumStatus.DENIED and workflow.state == WorkflowState.AWAITING_APPROVAL:
            workflow.transition_to(WorkflowState.DENIED)
            workflow.denied_at = self._clock.now()
            workflow.denial_reason = denial_reason

    @property
    def config(self) -> CouncilConfig:
        return self._config

    @property
    def ledger(self) -> LedgerChain:
        return self._ledger

    def register_agent(self, agent_id: str, role: AgentRole = AgentRole.APPLICATION) -> None:
        """Register an agent with a role."""
        self._agent_roles[agent_id] = role

    def register_council_member(self, member: CouncilMember) -> None:
        """Register a council member for quorum voting."""
        self._quorum_manager.register_member(member)

    def register_action_handler(self, handler: ActionHandler) -> None:
        """Register an action handler."""
        self._executor.register_handler(handler)

    def submit_intent(
        self,
        intent: Intent,
        agent_role: AgentRole | None = None,
        review_threshold: str | None = None,
    ) -> SubmitResult:
        """
        Submit an intent for evaluation and potential execution.

        Returns immediately with the initial evaluation result.
        For intents requiring approval, the workflow enters AWAITING_APPROVAL.
        """
        # Drop bookkeeping for long-finished workflows so the in-memory map
        # stays bounded for a long-lived council; their outcome remains in the
        # ledger and persistent store.
        self._prune_terminal_workflows()

        # The cap limits intents that are still in flight, not the lifetime
        # total handled by this council. Terminal workflows (completed, denied,
        # failed, expired, rolled back) are retained for status lookups but must
        # not count against the pending limit, otherwise a council would refuse
        # all new work once it had ever processed max_pending_intents intents.
        pending = sum(1 for workflow in self._workflows.values() if not workflow.is_terminal)
        if pending >= self._config.max_pending_intents:
            return SubmitResult(
                success=False,
                error="Maximum pending intents exceeded",
            )

        if self._config.require_justification and not intent.justification:
            return SubmitResult(
                success=False,
                error="Justification required for all intents",
            )

        role = agent_role or self._agent_roles.get(intent.agent_id, AgentRole.APPLICATION)

        workflow = IntentWorkflow(
            intent=intent,
            state=WorkflowState.SUBMITTED,
            submitted_at=self._clock.now(),
        )

        self._ledger.append(
            entry_type=LedgerEntryType.INTENT_SUBMITTED,
            payload=intent.to_dict(),
        )

        workflow.transition_to(WorkflowState.EVALUATING)
        evaluation = self._policy_engine.evaluate(intent, role)
        workflow.evaluation_result = evaluation
        workflow.evaluated_at = self._clock.now()

        decision_type = evaluation.decision.decision

        self._ledger.append(
            entry_type=LedgerEntryType.INTENT_EVALUATED,
            payload={
                "intent_id": intent.intent_id,
                "decision": decision_type.value,
                "explanation": evaluation.explanation,
            },
        )

        if decision_type == DecisionType.DENY:
            workflow.transition_to(WorkflowState.DENIED)
            workflow.denied_at = self._clock.now()
            workflow.denial_reason = evaluation.explanation
            self._workflows[intent.intent_id] = workflow
            return SubmitResult(
                success=False,
                workflow=workflow,
                error=f"Intent denied: {evaluation.explanation}",
            )

        if decision_type in (DecisionType.REQUIRE_APPROVAL, DecisionType.REQUIRE_QUORUM):
            workflow.transition_to(WorkflowState.AWAITING_APPROVAL)
            required_weight = self._resolve_required_weight(
                evaluation.decision.required_quorum_weight
                or evaluation.decision.required_approvers,
                review_threshold,
            )
            workflow.vote_record = self._quorum_manager.open_vote(
                intent_id=intent.intent_id,
                required_weight=required_weight,
            )
            workflow.metadata["review_threshold"] = review_threshold or "unanimous"
            self._workflows[intent.intent_id] = workflow
            return SubmitResult(
                success=True,
                workflow=workflow,
                requires_approval=True,
            )

        if decision_type == DecisionType.ALLOW:
            # When council members are registered, route through
            # AWAITING_APPROVAL so LLM evaluation and debate can still
            # occur.  Only auto-approve when there are no members
            # (headless / in-process mode).
            if self._quorum_manager.total_available_weight() > 0:
                workflow.transition_to(WorkflowState.AWAITING_APPROVAL)
                required_weight = self._resolve_required_weight(1, review_threshold)
                workflow.vote_record = self._quorum_manager.open_vote(
                    intent_id=intent.intent_id,
                    required_weight=required_weight,
                )
                workflow.metadata["review_threshold"] = review_threshold or "unanimous"
                self._workflows[intent.intent_id] = workflow
                return SubmitResult(
                    success=True,
                    workflow=workflow,
                    requires_approval=True,
                )

            workflow.transition_to(WorkflowState.APPROVED)
            workflow.approved_at = self._clock.now()

        self._workflows[intent.intent_id] = workflow
        return SubmitResult(success=True, workflow=workflow)

    def approve_intent(
        self,
        intent_id: str,
        approver_id: str,
        reason: str = "",
    ) -> SubmitResult:
        """Cast an approval vote for an intent."""
        self._prune_terminal_workflows()
        workflow = self._workflows.get(intent_id)
        if workflow is None:
            return SubmitResult(success=False, error="Intent not found")

        if workflow.state != WorkflowState.AWAITING_APPROVAL:
            return SubmitResult(
                success=False,
                workflow=workflow,
                error=f"Intent is not awaiting approval (state: {workflow.state.value})",
            )

        return self.cast_member_vote(intent_id, approver_id, VoteType.APPROVE, reason=reason)

    def deny_intent(
        self,
        intent_id: str,
        denier_id: str,
        reason: str = "",
    ) -> SubmitResult:
        """Cast a denial vote for an intent."""
        self._prune_terminal_workflows()
        workflow = self._workflows.get(intent_id)
        if workflow is None:
            return SubmitResult(success=False, error="Intent not found")

        if workflow.state != WorkflowState.AWAITING_APPROVAL:
            return SubmitResult(
                success=False,
                workflow=workflow,
                error=f"Intent is not awaiting approval (state: {workflow.state.value})",
            )

        return self.cast_member_vote(intent_id, denier_id, VoteType.DENY, reason=reason)

    def cast_member_vote(
        self,
        intent_id: str,
        voter_id: str,
        vote_type: VoteType,
        reason: str = "",
        *,
        finalize: bool = True,
    ) -> SubmitResult:
        """Record a council member vote and optionally finalize the review."""
        self._prune_terminal_workflows()
        workflow = self._workflows.get(intent_id)
        if workflow is None:
            return SubmitResult(success=False, error="Intent not found")

        if workflow.state != WorkflowState.AWAITING_APPROVAL:
            return SubmitResult(
                success=False,
                workflow=workflow,
                error=f"Intent is not awaiting approval (state: {workflow.state.value})",
            )

        result = self._quorum_manager.cast_vote(
            intent_id=intent_id,
            voter_id=voter_id,
            vote_type=vote_type,
            reason=reason,
            auto_close=finalize,
        )

        workflow.vote_record = result.record
        self._apply_quorum_result(workflow, result.status, reason)
        return SubmitResult(success=True, workflow=workflow)

    def revise_vote(
        self,
        intent_id: str,
        voter_id: str,
        vote_type_str: str,
        reason: str = "",
        debate_round: int = 0,
        *,
        finalize: bool = True,
    ) -> SubmitResult:
        """Revise a vote during a debate round.

        Unlike ``approve_intent``/``deny_intent``, this allows a member
        who already voted to change their position and records the change
        as a ``DEBATE_VOTE`` ledger entry.
        """
        self._prune_terminal_workflows()
        workflow = self._workflows.get(intent_id)
        if workflow is None:
            return SubmitResult(success=False, error="Intent not found")

        if workflow.state != WorkflowState.AWAITING_APPROVAL:
            return SubmitResult(
                success=False,
                workflow=workflow,
                error=f"Intent is not awaiting approval (state: {workflow.state.value})",
            )

        from nomos.level_2.quorum.vote import VoteType

        vt = VoteType.APPROVE if vote_type_str == "approve" else VoteType.DENY
        result = self._quorum_manager.revise_vote(
            intent_id=intent_id,
            voter_id=voter_id,
            vote_type=vt,
            reason=reason,
            debate_round=debate_round,
            auto_close=finalize,
        )

        workflow.vote_record = result.record
        self._apply_quorum_result(workflow, result.status, reason)
        return SubmitResult(success=True, workflow=workflow)

    def finalize_intent_review(
        self, intent_id: str, *, allow_pending: bool = False
    ) -> SubmitResult:
        """Finalize a batched council review round and sync workflow state."""
        self._prune_terminal_workflows()
        workflow = self._workflows.get(intent_id)
        if workflow is None:
            return SubmitResult(success=False, error="Intent not found")

        if workflow.state != WorkflowState.AWAITING_APPROVAL:
            return SubmitResult(success=True, workflow=workflow)

        result = self._quorum_manager.finalize_vote(intent_id, allow_pending=allow_pending)
        workflow.vote_record = result.record
        self._apply_quorum_result(workflow, result.status, workflow.denial_reason)
        return SubmitResult(success=True, workflow=workflow)

    def mint_token(
        self,
        intent_id: str,
        ttl_seconds: int | None = None,
    ) -> CapabilityToken | None:
        """
        Mint a capability token for an approved intent.

        The token authorizes execution of the action described in the intent.
        """
        self._prune_terminal_workflows()
        workflow = self._workflows.get(intent_id)
        if workflow is None:
            return None

        if workflow.state != WorkflowState.APPROVED:
            return None

        intent = workflow.intent
        scope = CapabilityScope(
            action_type=intent.action_type,
            resource_pattern=intent.target_resource,
            allowed_operations=frozenset(["execute", "rollback"]),
            max_invocations=2,
        )

        mint_result = self._capability_service.mint(
            MintRequest(
                intent=intent,
                scope=scope,
                ttl_seconds=ttl_seconds or self._config.default_token_ttl_seconds,
            )
        )

        if mint_result.success and mint_result.token:
            workflow.capability_token = mint_result.token
            workflow.token_minted_at = self._clock.now()
            workflow.transition_to(WorkflowState.TOKEN_MINTED)
            if mint_result.ledger_sequence:
                workflow.record_ledger_sequence(mint_result.ledger_sequence)
            return mint_result.token

        return None

    def execute_intent(self, intent_id: str) -> ExecuteResult:
        """
        Execute an approved intent with its capability token.

        This is the complete flow: execute -> verify -> complete/rollback.
        """
        self._prune_terminal_workflows()
        workflow = self._workflows.get(intent_id)
        if workflow is None:
            return ExecuteResult(success=False, error="Intent not found")

        if workflow.state == WorkflowState.APPROVED:
            token = self.mint_token(intent_id)
            if token is None:
                return ExecuteResult(
                    success=False,
                    workflow=workflow,
                    error="Failed to mint capability token",
                )

        if workflow.state != WorkflowState.TOKEN_MINTED:
            return ExecuteResult(
                success=False,
                workflow=workflow,
                error=f"Intent not ready for execution (state: {workflow.state.value})",
            )

        if workflow.capability_token is None:
            return ExecuteResult(
                success=False,
                workflow=workflow,
                error="No capability token available",
            )

        intent = workflow.intent
        action = Action(
            action_type=intent.action_type,
            resource=intent.target_resource,
            operation="execute",
            parameters=intent.parameters,
            metadata={"intent_id": intent.intent_id},
        )

        workflow.transition_to(WorkflowState.EXECUTING)
        execution_result = self._executor.execute(action, workflow.capability_token.token_id)
        workflow.execution_result = execution_result
        workflow.executed_at = self._clock.now()

        if execution_result.ledger_sequence:
            workflow.record_ledger_sequence(execution_result.ledger_sequence)

        if not execution_result.succeeded:
            workflow.transition_to(WorkflowState.FAILED)
            workflow.error = execution_result.error
            workflow.completed_at = self._clock.now()
            return ExecuteResult(
                success=False,
                workflow=workflow,
                error=execution_result.error,
            )

        workflow.transition_to(WorkflowState.EXECUTED)

        workflow.transition_to(WorkflowState.VERIFYING)
        verification_result = self._verifier.verify(action, execution_result)
        workflow.verification_result = verification_result
        workflow.verified_at = self._clock.now()

        if verification_result.ledger_sequence:
            workflow.record_ledger_sequence(verification_result.ledger_sequence)

        if verification_result.status == VerificationStatus.PASSED:
            workflow.transition_to(WorkflowState.VERIFIED)
            workflow.transition_to(WorkflowState.COMPLETED)
            workflow.completed_at = self._clock.now()
            return ExecuteResult(success=True, workflow=workflow)

        workflow.transition_to(WorkflowState.VERIFICATION_FAILED)

        if self._verifier.should_rollback(verification_result):
            if self._executor.can_rollback(action.action_type):
                workflow.transition_to(WorkflowState.ROLLING_BACK)
                rollback_result = self._executor.rollback(
                    action,
                    execution_result,
                    workflow.capability_token.token_id,
                )
                workflow.rollback_result = rollback_result
                workflow.rolled_back_at = self._clock.now()

                if rollback_result.succeeded:
                    workflow.transition_to(WorkflowState.ROLLED_BACK)
                else:
                    workflow.transition_to(WorkflowState.FAILED)
                    workflow.error = f"Rollback failed: {rollback_result.error}"
            else:
                workflow.transition_to(WorkflowState.FAILED)
                workflow.error = "Verification failed and rollback not supported"
        else:
            workflow.transition_to(WorkflowState.COMPLETED)

        workflow.completed_at = self._clock.now()
        return ExecuteResult(
            success=workflow.state == WorkflowState.COMPLETED,
            workflow=workflow,
            error=workflow.error,
        )

    def get_workflow(self, intent_id: str) -> IntentWorkflow | None:
        """Get workflow by intent ID."""
        self._prune_terminal_workflows()
        return self._workflows.get(intent_id)

    def get_pending_approvals(self) -> list[IntentWorkflow]:
        """Get all workflows awaiting approval."""
        self._prune_terminal_workflows()
        return [w for w in self._workflows.values() if w.state == WorkflowState.AWAITING_APPROVAL]

    def get_active_workflows(self) -> list[IntentWorkflow]:
        """Get all non-terminal workflows."""
        self._prune_terminal_workflows()
        return [w for w in self._workflows.values() if not w.is_terminal]

    def validate_ledger(self) -> bool:
        """Validate the integrity of the ledger."""
        return self._ledger.validate()

    def get_ledger_status(self) -> dict[str, Any]:
        """Get current ledger status."""
        status = self._ledger.status()
        return {
            "chain_id": status.chain_id,
            "length": status.length,
            "head_sequence": status.head_sequence,
            "head_hash": str(status.head_hash) if status.head_hash else None,
            "is_valid": status.is_valid,
        }
