# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""A decision deadline holds wherever the decision is used, not only where it is swept.

Loop-owned maintenance expires a bounded batch per call, and a writ's validity
window is fixed before its signature is requested. Each test puts the deadline
between a bounded sweep, or a slow signature, and the use of the decision.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from nomos.auth import writ_factory
from nomos.auth.writ_factory import WritFactory, WritSigningExpiredError
from nomos.crypto.signer_provider import InMemorySigner
from nomos.level_0.clock import MockClock
from nomos.level_0.crypto.signing import KeyPair
from nomos.level_0.types import CouncilMember, Intent
from nomos.level_2.quorum.vote import VoteType
from nomos.level_4.council.workflow import WorkflowState
from nomos.llm.council_agent import IntentEvaluation
from nomos.nat.functions import AuthorizeInput, CouncilService
from tests.test_parallel_council_review import _add_member, _approval, _proposal, _service
from tests.test_telemetry_sign_isolation import settings

_PAST_FALLBACK_DEADLINE = 301


def _intent(tag: str) -> Intent:
    return Intent.create(
        agent_id="agent",
        action_type="system.noop",
        description=f"Deadline review {tag}",
        target_resource="test",
        justification="Hold a decision across its deadline",
    )


def _one_entry_sweeps(service: CouncilService) -> MockClock:
    """Sweep one workflow per call, so most entries are outside any one batch."""
    clock = MockClock(datetime.now(UTC))
    service.council._clock = clock
    service.council.config.workflow_cleanup_batch = 1
    return clock


def _last_swept(service: CouncilService, count: int = 1) -> list[str]:
    """The workflows the bounded sweep will reach last."""
    return list(service.council._cleanup_ids)[-count:]


async def _approved(service: CouncilService, count: int) -> list[str]:
    async def approve(_intent: Intent) -> IntentEvaluation:
        return _approval()

    _add_member(service, "member", approve)
    ids = []
    for index in range(count):
        result = await service.propose_intent(_proposal(str(index)))
        assert result.success, result.error
        ids.append(result.intent_id)
    assert all(service.council._workflows[key].state == WorkflowState.APPROVED for key in ids)
    return ids


async def _awaiting(service: CouncilService, count: int) -> list[str]:
    service.council.register_council_member(
        CouncilMember(member_id="manual", public_key_hex="", display_name="Manual")
    )
    ids = []
    for index in range(count):

        async def submit(index=index):
            return service._submit_intent(_intent(str(index)))

        result = await service._run_workflow_request(submit(), "propose")
        assert result.workflow.state == WorkflowState.AWAITING_APPROVAL
        ids.append(result.workflow.intent_id)
    return ids


@pytest.mark.asyncio
async def test_no_token_for_an_elapsed_approval_outside_the_cleanup_batch():
    service = _service()
    clock = _one_entry_sweeps(service)
    await _approved(service, 6)
    (target,) = _last_swept(service)
    ledger_length = service.council.ledger.status().length
    clock.advance(seconds=_PAST_FALLBACK_DEADLINE)

    # The one-entry sweep inside mint_token reaches a different workflow.
    assert service.council.mint_token(target) is None

    assert service.council._workflows[target].state == WorkflowState.EXPIRED
    assert service.council.ledger.status().length == ledger_length
    await service.aclose_workflows()


@pytest.mark.asyncio
async def test_no_execution_of_an_elapsed_approval_outside_the_cleanup_batch():
    service = _service()
    clock = _one_entry_sweeps(service)
    await _approved(service, 6)
    (target,) = _last_swept(service)
    clock.advance(seconds=_PAST_FALLBACK_DEADLINE)

    result = service.council.execute_intent(target)

    assert not result.success
    assert service.council._workflows[target].state == WorkflowState.EXPIRED
    assert service.council._workflows[target].capability_token is None
    await service.aclose_workflows()


@pytest.mark.asyncio
async def test_no_vote_on_an_elapsed_review_outside_the_cleanup_batch():
    service = _service()
    clock = _one_entry_sweeps(service)
    await _awaiting(service, 8)
    first, second = _last_swept(service, 2)
    clock.advance(seconds=_PAST_FALLBACK_DEADLINE)

    approval = service.council.approve_intent(first, "manual")
    vote = service.council.cast_member_vote(second, "manual", VoteType.APPROVE)

    assert not approval.success and "expired" in approval.error
    assert not vote.success and "expired" in vote.error
    assert service.council._workflows[first].state == WorkflowState.EXPIRED
    assert service.council._workflows[second].state == WorkflowState.EXPIRED
    await service.aclose_workflows()


@pytest.mark.asyncio
async def test_listings_hold_no_elapsed_entry_whatever_the_cleanup_batch():
    service = _service()
    clock = _one_entry_sweeps(service)
    ids = await _awaiting(service, 4)
    assert {w.intent_id for w in service.council.get_pending_approvals()} == set(ids)
    assert {w.intent_id for w in service.council.get_active_workflows()} == set(ids)
    clock.advance(seconds=_PAST_FALLBACK_DEADLINE)

    assert service.council.get_pending_approvals() == []
    assert service.council.get_active_workflows() == []
    await service.aclose_workflows()


@pytest.mark.asyncio
async def test_pending_list_skips_another_replicas_elapsed_row():
    service = _service()
    clock = _one_entry_sweeps(service)
    (intent_id,) = await _awaiting(service, 1)
    row = service.council._workflows.pop(intent_id).to_durable_dict()
    service.council._workflow_store = SimpleNamespace(
        list_workflows_by_state=lambda states, **_: [row]
    )
    assert [w.intent_id for w in service.council.get_pending_approvals()] == [intent_id]

    clock.advance(seconds=_PAST_FALLBACK_DEADLINE)

    assert service.council.get_pending_approvals() == []
    await service.aclose_workflows()


@pytest.mark.asyncio
async def test_pending_list_skips_a_row_that_elapses_during_the_store_read():
    service = _service()
    clock = _one_entry_sweeps(service)
    (intent_id,) = await _awaiting(service, 1)
    row = service.council._workflows.pop(intent_id).to_durable_dict()

    def read_past_the_deadline(states, **_):
        clock.advance(seconds=_PAST_FALLBACK_DEADLINE)  # time spent waiting for the store
        return [row]

    service.council._workflow_store = SimpleNamespace(
        list_workflows_by_state=read_past_the_deadline
    )

    assert service.council.get_pending_approvals() == []
    await service.aclose_workflows()


class _RecordingStore:
    def __init__(self) -> None:
        self.writs: list[str] = []
        self.intents: list[str] = []

    async def store_writ(self, writ_id, _data) -> None:
        self.writs.append(writ_id)

    async def store_intent(self, intent_id, _data) -> None:
        self.intents.append(intent_id)


def _command_intent() -> Intent:
    return Intent.create(
        agent_id="test",
        action_type="system.info",
        description="Sign after the deadline",
        target_resource="/workspace",
        justification="Test",
        command="pwd",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("queued_for", "issued"), [(31, False), (5, True)])
async def test_writ_that_expires_while_queued_for_signing_is_not_issued(
    monkeypatch, queued_for, issued
):
    now = [1_000_000.0]
    monkeypatch.setattr(writ_factory, "time", SimpleNamespace(time=lambda: now[0]))
    signer = InMemorySigner(KeyPair.generate(key_id="test"))
    store = _RecordingStore()

    async def slow_sign(provider, payload):
        now[0] += queued_for  # time spent waiting for a signing worker
        return provider.sign(payload)

    factory = WritFactory(signer=signer, store=store, sign_async=slow_sign)
    # A critical-risk writ is valid for 30 seconds from the moment minting starts.
    minting = factory.mint_writ(_command_intent(), "test", "quorum", [], "critical")

    if issued:
        writ = await minting
        assert writ.expires_at > now[0] and store.writs == [writ.writ_id]
    else:
        with pytest.raises(WritSigningExpiredError, match="no writ issued"):
            await minting
        assert store.writs == [] and store.intents == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("stored_for", "issued"), [(31, False), (5, True)])
async def test_writ_that_expires_while_being_stored_is_not_issued(monkeypatch, stored_for, issued):
    now = [1_000_000.0]
    monkeypatch.setattr(writ_factory, "time", SimpleNamespace(time=lambda: now[0]))
    signer = InMemorySigner(KeyPair.generate(key_id="test"))

    class SlowStore(_RecordingStore):
        async def store_writ(self, writ_id, data) -> None:
            now[0] += stored_for  # time spent waiting for the store
            await super().store_writ(writ_id, data)

    factory = WritFactory(signer=signer, store=SlowStore())
    # A critical-risk writ is valid for 30 seconds from the moment minting starts.
    minting = factory.mint_writ(_command_intent(), "test", "quorum", [], "critical")

    if issued:
        assert (await minting).expires_at > now[0]
    else:
        with pytest.raises(WritSigningExpiredError, match="no writ issued"):
            await minting


@pytest.mark.asyncio
async def test_writ_is_not_stored_when_its_approval_lapsed_during_signing():
    signer = InMemorySigner(KeyPair.generate(key_id="test"))
    store = _RecordingStore()
    factory = WritFactory(signer=signer, store=store)

    with pytest.raises(WritSigningExpiredError, match="approval expired during writ mint"):
        await factory.mint_writ(
            _command_intent(), "test", "quorum", [], "low", still_approved=lambda: False
        )

    assert store.writs == [] and store.intents == []


def _authorizing_service() -> CouncilService:
    service = CouncilService(settings(enabled=False, redis_enabled=False))
    signer = InMemorySigner(KeyPair.generate(key_id="test"))
    service._signer_provider = signer
    service.council.ledger._signer = signer

    class Member:
        member_id = display_name = "member"
        provider_name = "offline"

        async def evaluate_intent(self, intent):
            return IntentEvaluation(decision="approve", reasoning="offline")

    service._agents = [Member()]
    service.council.register_council_member(CouncilMember("member", "", "Member"))
    return service


def _authorize_input() -> AuthorizeInput:
    return AuthorizeInput(
        council_id="isolation",
        agent_id="test",
        command="pwd",
        working_directory="/workspace",
        description="offline review",
        target_resource="/workspace",
        justification="test",
    )


@pytest.mark.asyncio
async def test_authorize_reports_a_writ_that_expired_in_the_signing_queue(monkeypatch):
    service = _authorizing_service()
    real_sign = service._writ_signing.sign
    now = [1_000_000.0]
    monkeypatch.setattr(writ_factory, "time", SimpleNamespace(time=lambda: now[0]))

    async def queued_past_the_window(signer, payload):
        now[0] += 10_000
        return await real_sign(signer, payload)

    service._writ_signing.sign = queued_past_the_window
    try:
        result = await service.authorize(_authorize_input())
        assert result.status == "error" and not result.writ
        assert "expired while waiting for its signature" in result.error
        # The review itself was decided and audited; only the writ is missing.
        assert service.council.ledger.get_review_by_intent(result.decision_id) is not None
    finally:
        service._writ_signing.sign = real_sign
        await service.aclose()


@pytest.mark.asyncio
async def test_authorize_withholds_the_writ_when_approval_lapses_during_signing():
    service = _authorizing_service()
    clock = MockClock(datetime.now(UTC))
    service.council._clock = clock
    real_sign = service._writ_signing.sign

    async def approval_lapses_meanwhile(signer, payload):
        clock.advance(seconds=_PAST_FALLBACK_DEADLINE)
        return await real_sign(signer, payload)

    service._writ_signing.sign = approval_lapses_meanwhile
    try:
        result = await service.authorize(_authorize_input())
        assert result.status == "error" and not result.writ
        assert "expired during writ mint" in result.error
        workflow = service.council.get_workflow(result.decision_id)
        assert workflow is not None and workflow.state == WorkflowState.EXPIRED
        assert "writ_id" not in workflow.metadata
    finally:
        service._writ_signing.sign = real_sign
        await service.aclose()
