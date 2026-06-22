# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


"""Regression tests for the ``max_pending_intents`` cap.

The cap must bound only intents that are still *in flight* (non-terminal), not
the lifetime total of intents a council has ever handled. Before the fix the
guard counted every workflow ever submitted — including completed and denied
ones, which were never evicted — so a long-lived council started rejecting all
new work with "Maximum pending intents exceeded" once it had processed
``max_pending_intents`` intents, and the workflow map grew without bound.
"""

from nomos.level_0.clock import MockClock
from nomos.level_0.types import AgentRole, CouncilMember, Intent
from nomos.level_3.executor import NoOpHandler
from nomos.level_4.council import CouncilConfig, SecurityCouncil, WorkflowState


def _make_council(
    max_pending: int,
    *,
    clock=None,
    default_token_ttl_seconds: int = 300,
) -> SecurityCouncil:
    config = CouncilConfig(
        council_id="test-council",
        require_justification=True,
        max_pending_intents=max_pending,
        default_token_ttl_seconds=default_token_ttl_seconds,
    )
    council = SecurityCouncil(config=config, clock=clock)
    council.register_action_handler(NoOpHandler())
    council.register_agent("agent-1", AgentRole.APPLICATION)
    council.register_council_member(
        CouncilMember(member_id="alice", public_key_hex="abc", display_name="Alice", weight=1)
    )
    council.register_council_member(
        CouncilMember(member_id="bob", public_key_hex="def", display_name="Bob", weight=1)
    )
    return council


def _noop_intent(tag: str) -> Intent:
    return Intent.create(
        agent_id="agent-1",
        action_type="system.noop",
        description=f"No-op action {tag} for testing",
        target_resource="/tmp/test",
        justification="Testing the pending-intents cap",
    )


def _run_to_completion(council: SecurityCouncil, tag: str) -> str:
    """Submit a no-op and drive it to a terminal state via manual approval."""
    intent = _noop_intent(tag)
    submit = council.submit_intent(intent)
    assert submit.success, submit.error
    assert submit.requires_approval
    council.approve_intent(intent.intent_id, "alice")
    council.approve_intent(intent.intent_id, "bob")
    execute = council.execute_intent(intent.intent_id)
    assert execute.success, execute.error
    assert council.get_workflow(intent.intent_id).is_terminal
    return intent.intent_id


def test_completed_intents_do_not_count_toward_pending_cap():
    """A council that has completed more intents than the cap must still accept
    new submissions — completed workflows are not 'pending'."""
    max_pending = 3
    council = _make_council(max_pending)

    # Complete well more than the cap over the council's lifetime. On the buggy
    # implementation the (max_pending + 1)th submission fails outright.
    for i in range(max_pending + 5):
        intent_id = _run_to_completion(council, f"hist-{i}")
        assert council.get_workflow(intent_id) is not None

    # A fresh submission still succeeds despite the lifetime total.
    result = council.submit_intent(_noop_intent("fresh"))
    assert result.success, result.error


def test_terminal_workflows_are_pruned_to_stay_bounded():
    """Terminal workflows are retained for status lookups, but the in-memory
    map is bounded so a long-lived council does not leak."""
    max_pending = 3
    council = _make_council(max_pending)

    last_intent_id = ""
    for i in range(50):
        last_intent_id = _run_to_completion(council, f"bulk-{i}")

    # Retained history is bounded by the cap (plus at most the just-submitted
    # in-flight workflow), not the 50 intents handled over the lifetime.
    assert len(council._workflows) <= max_pending + 1
    # The most recent completed intent is still queryable for status.
    assert council.get_workflow(last_intent_id) is not None


def test_pending_cap_still_rejects_too_many_in_flight():
    """The cap still protects against unbounded in-flight (non-terminal)
    intents."""
    max_pending = 2
    council = _make_council(max_pending)

    # system.noop auto-approves (no members); APPROVED is non-terminal, so these
    # remain 'pending' until executed.
    for i in range(max_pending):
        result = council.submit_intent(_noop_intent(f"pending-{i}"))
        assert result.success, result.error
        assert not council.get_workflow(result.workflow.intent.intent_id).is_terminal

    overflow = council.submit_intent(_noop_intent("overflow"))
    assert overflow.success is False
    assert "Maximum pending intents exceeded" in overflow.error


def test_approved_decisions_expire_and_stop_counting_toward_pending_cap():
    """Decision-service approvals eventually stop consuming in-flight capacity."""
    max_pending = 2
    clock = MockClock()
    council = _make_council(
        max_pending,
        clock=clock,
        default_token_ttl_seconds=10,
    )
    approved_ids: list[str] = []

    for i in range(max_pending):
        intent = _noop_intent(f"approved-{i}")
        submit = council.submit_intent(intent)
        assert submit.success, submit.error
        council.approve_intent(intent.intent_id, "alice")
        council.approve_intent(intent.intent_id, "bob")

        workflow = council.get_workflow(intent.intent_id)
        assert workflow is not None
        assert workflow.state == WorkflowState.APPROVED
        assert not workflow.is_terminal
        approved_ids.append(intent.intent_id)

    overflow = council.submit_intent(_noop_intent("pre-expiry-overflow"))
    assert overflow.success is False
    assert "Maximum pending intents exceeded" in overflow.error

    clock.advance(seconds=10)
    fresh = council.submit_intent(_noop_intent("fresh-after-expiry"))
    assert fresh.success, fresh.error

    for intent_id in approved_ids:
        workflow = council.get_workflow(intent_id)
        assert workflow is not None
        assert workflow.state == WorkflowState.EXPIRED
        assert workflow.is_terminal


def test_approved_decisions_honor_explicit_decision_expiry_metadata():
    """Writ-producing paths can attach the writ expiry to the workflow."""
    clock = MockClock()
    council = _make_council(1, clock=clock, default_token_ttl_seconds=300)
    intent = _noop_intent("metadata-expiry")
    submit = council.submit_intent(intent)
    assert submit.success, submit.error
    council.approve_intent(intent.intent_id, "alice")
    council.approve_intent(intent.intent_id, "bob")

    workflow = council.get_workflow(intent.intent_id)
    assert workflow is not None
    workflow.metadata["decision_expires_at"] = clock.now_unix() + 1

    clock.advance(seconds=1)
    expired = council.get_workflow(intent.intent_id)
    assert expired is not None
    assert expired.state == WorkflowState.EXPIRED
    assert expired.is_terminal
