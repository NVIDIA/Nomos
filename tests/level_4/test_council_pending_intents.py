"""Regression tests for the ``max_pending_intents`` cap.

The cap must bound only intents that are still *in flight* (non-terminal), not
the lifetime total of intents a council has ever handled. Before the fix the
guard counted every workflow ever submitted — including completed and denied
ones, which were never evicted — so a long-lived council started rejecting all
new work with "Maximum pending intents exceeded" once it had processed
``max_pending_intents`` intents, and the workflow map grew without bound.
"""

from nomos.level_0.types import AgentRole, CouncilMember, Intent
from nomos.level_3.executor import NoOpHandler
from nomos.level_4.council import CouncilConfig, SecurityCouncil


def _make_council(max_pending: int) -> SecurityCouncil:
    config = CouncilConfig(
        council_id="test-council",
        require_justification=True,
        max_pending_intents=max_pending,
    )
    council = SecurityCouncil(config=config)
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
