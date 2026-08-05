# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

"""Admission-reservation tests for parallel council review preflights."""

from dataclasses import replace

from nomos.level_0.clock import MockClock
from nomos.level_0.types import AgentRole, Intent
from nomos.level_1.policy import DecisionType, PolicyRule
from nomos.level_4.council import CouncilConfig, SecurityCouncil


def _intent(tag: str) -> Intent:
    return Intent.create(
        agent_id="agent-1",
        action_type="system.noop",
        description=f"Preflight reservation test {tag}",
        target_resource="/tmp/preflight",
        justification="Verify admission is reserved before paid review",
    )


def _council(*, max_pending: int = 1, clock=None, ttl: int = 5) -> SecurityCouncil:
    council = SecurityCouncil(
        CouncilConfig(
            council_id="preflight-test",
            max_pending_intents=max_pending,
            preflight_ttl_seconds=ttl,
        ),
        clock=clock,
    )
    council.register_agent("agent-1", AgentRole.APPLICATION)
    return council


def test_preflight_writes_nothing_and_policy_is_reused_once():
    council = _council()
    intent = _intent("single-policy-evaluation")
    original_evaluate = council._policy_engine.evaluate
    calls = 0

    def counted_evaluate(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_evaluate(*args, **kwargs)

    council._policy_engine.evaluate = counted_evaluate
    ledger_length = council.ledger.length()

    checked = council.preflight_intent(intent)

    assert checked.success
    assert checked.preflight is not None
    assert calls == 1
    assert council.ledger.length() == ledger_length
    assert council.get_workflow(intent.intent_id) is None

    submitted = council.submit_intent(intent, preflight=checked.preflight)
    assert submitted.success
    assert calls == 1


def test_preflight_is_single_use_and_bound_before_ledger_mutation():
    council = _council()
    intent = _intent("binding")
    checked = council.preflight_intent(intent)
    assert checked.preflight is not None
    ledger_length = council.ledger.length()

    forged = replace(checked.preflight, intent_id=_intent("other").intent_id)
    mismatched = council.submit_intent(intent, preflight=forged)
    assert not mismatched.success
    assert council.ledger.length() == ledger_length

    role_mismatch = council.submit_intent(
        intent,
        agent_role=AgentRole.ADMIN,
        preflight=checked.preflight,
    )
    assert not role_mismatch.success
    assert council.ledger.length() == ledger_length

    submitted = council.submit_intent(intent, preflight=checked.preflight)
    assert submitted.success
    ledger_after_commit = council.ledger.length()

    reused = council.submit_intent(intent, preflight=checked.preflight)
    assert not reused.success
    assert council.ledger.length() == ledger_after_commit


def test_reservations_count_toward_cap_and_release_idempotently():
    council = _council(max_pending=1)
    first = council.preflight_intent(_intent("first"))
    assert first.success and first.preflight is not None

    blocked = council.preflight_intent(_intent("blocked"))
    assert not blocked.success
    assert blocked.error == "Maximum pending intents exceeded"

    council.release_preflight(first.preflight)
    council.release_preflight(first.preflight)
    admitted = council.preflight_intent(_intent("admitted"))
    assert admitted.success


def test_policy_deny_preflight_counts_toward_cap_until_released():
    council = _council(max_pending=1)
    council._policy_engine.add_rule(
        PolicyRule(
            rule_id="deny-preflight-cap",
            name="Deny preflight cap test actions",
            priority=0,
            action_types={"danger.read"},
            decision=DecisionType.DENY,
        )
    )
    denied = _intent("denied-reservation").model_copy(update={"action_type": "danger.read"})
    first = council.preflight_intent(denied)
    assert first.success and first.preflight is not None
    assert first.preflight.policy_evaluation.decision.decision == DecisionType.DENY

    blocked = council.preflight_intent(denied.model_copy(update={"intent_id": "other"}))
    assert not blocked.success
    assert blocked.error == "Maximum pending intents exceeded"

    council.release_preflight(first.preflight)
    replacement = council.preflight_intent(denied.model_copy(update={"intent_id": "replacement"}))
    assert replacement.success


def test_expired_reservation_is_rejected_and_capacity_is_reclaimed():
    clock = MockClock()
    council = _council(clock=clock, ttl=5)
    intent = _intent("expired")
    checked = council.preflight_intent(intent)
    assert checked.preflight is not None

    clock.advance(seconds=5)
    expired = council.submit_intent(intent, preflight=checked.preflight)
    assert not expired.success
    assert "expired" in (expired.error or "").lower()
    assert council.get_workflow(intent.intent_id) is None

    replacement = council.preflight_intent(_intent("replacement"))
    assert replacement.success


def test_direct_submit_without_preflight_remains_compatible():
    council = _council()
    result = council.submit_intent(_intent("legacy-direct"))
    assert result.success
    assert result.workflow is not None


def test_preflight_rejects_changed_policy_relevant_intent_content():
    council = _council()
    council._policy_engine.add_rule(
        PolicyRule(
            rule_id="deny-danger",
            name="Deny dangerous action",
            priority=0,
            action_types={"danger.read"},
            decision=DecisionType.DENY,
        )
    )
    safe = _intent("content-binding")
    checked = council.preflight_intent(safe)
    assert checked.preflight is not None
    ledger_length = council.ledger.length()

    changed = safe.model_copy(update={"action_type": "danger.read"})
    submitted = council.submit_intent(changed, preflight=checked.preflight)

    assert not submitted.success
    assert submitted.error == "Intent preflight content mismatch"
    assert council.get_workflow(safe.intent_id) is None
    assert council.ledger.length() == ledger_length


def test_preflight_uses_server_owned_policy_result_when_public_copy_is_mutated():
    council = _council()
    council._policy_engine.add_rule(
        PolicyRule(
            rule_id="deny-danger",
            name="Deny dangerous action",
            priority=0,
            action_types={"danger.read"},
            decision=DecisionType.DENY,
        )
    )
    dangerous = _intent("evaluation-binding").model_copy(update={"action_type": "danger.read"})
    checked = council.preflight_intent(dangerous)
    assert checked.preflight is not None
    assert checked.preflight.policy_evaluation.decision.decision == DecisionType.DENY

    checked.preflight.policy_evaluation.decision.decision = DecisionType.REQUIRE_APPROVAL
    submitted = council.submit_intent(dangerous, preflight=checked.preflight)

    assert not submitted.success
    assert submitted.workflow is not None
    assert submitted.workflow.state.value == "denied"
    assert submitted.workflow.evaluation_result is not None
    assert submitted.workflow.evaluation_result.decision.decision == DecisionType.DENY
