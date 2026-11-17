# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""A decision deadline holds wherever the decision is used, not only where it is swept.

Loop-owned maintenance expires a bounded batch per call, and a writ's validity
window is fixed before its signature is requested. Each test puts the deadline
between a bounded sweep, or a slow signature, and the use of the decision.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from nomos.auth import writ_factory
from nomos.auth.writ_factory import WritFactory, WritSigningExpiredError
from nomos.config.settings import ChairmanConfig, CouncilSettings, OPAConfig
from nomos.crypto.signer_provider import InMemorySigner
from nomos.level_0.clock import MockClock
from nomos.level_0.crypto.signing import KeyPair
from nomos.level_0.types import CouncilMember, Intent
from nomos.level_1.ledger import LedgerEntry
from nomos.level_2.quorum.vote import VoteType
from nomos.level_4.council.workflow import IntentWorkflow, WorkflowState
from nomos.llm.council_agent import ChairmanDecision, ChairmanPreAuthDecision, IntentEvaluation
from nomos.nat.functions import AuthorizeInput, CouncilService
from tests.postgres_test_support import (
    ensure_migrated_and_clean,
    postgres_dsn_for_tests,
    postgres_unavailable_reason,
)
from tests.test_parallel_council_review import (
    FakeChairman,
    _add_member,
    _approval,
    _proposal,
    _service,
)
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


@pytest.mark.asyncio
@pytest.mark.parametrize("lapses", [True, False])
async def test_writ_is_withheld_when_its_approval_lapses_while_being_stored(lapses):
    signer = InMemorySigner(KeyPair.generate(key_id="test"))
    approved = [True]

    class SlowStore(_RecordingStore):
        async def store_writ(self, writ_id, data) -> None:
            approved[0] = not lapses  # the approval deadline passes meanwhile
            await super().store_writ(writ_id, data)

    store = SlowStore()
    factory = WritFactory(signer=signer, store=store)
    minting = factory.mint_writ(
        _command_intent(), "test", "quorum", [], "low", still_approved=lambda: approved[0]
    )

    if lapses:
        with pytest.raises(WritSigningExpiredError, match="approval expired during writ mint"):
            await minting
        # The approval still stood when the signature arrived: it lapsed in the store.
        assert len(store.writs) == 1
    else:
        assert store.writs == [(await minting).writ_id]


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
@pytest.mark.parametrize(("flushed_for", "issued"), [(10_000, False), (5, True)])
async def test_authorize_withholds_a_writ_that_expires_during_the_final_flush(
    monkeypatch, flushed_for, issued
):
    service = _authorizing_service()
    real_flush = service._workflow_runtime.flush
    now = [1_000_000.0]
    monkeypatch.setattr(writ_factory, "time", SimpleNamespace(time=lambda: now[0]))
    flushed = []

    async def slow_flush(intent_id, version):
        now[0] += flushed_for  # time spent waiting for the store
        flushed.append(intent_id)
        return await real_flush(intent_id, version)

    service._workflow_runtime.flush = slow_flush
    try:
        result = await service.authorize(_authorize_input())
        assert flushed == [result.decision_id]
        if issued:
            assert result.status == "approved"
            assert WritFactory.deserialize_writ(result.writ).expires_at > now[0]
        else:
            assert result.status == "error" and not result.writ and not result.writ_id
            assert "no writ issued. Retry the request." in result.error
        # The review itself was decided and audited either way.
        review = service.council.ledger.get_review_by_intent(result.decision_id)
        assert review is not None and review.payload["final"]["decision"] == "approve"
    finally:
        service._workflow_runtime.flush = real_flush
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


def _debate_outlasts_the_deadline(
    service: CouncilService, hold: Callable[[], Awaitable[None]] | None = None
) -> None:
    """Members split on the first round, then agree to approve only after the deadline."""
    clock = MockClock(datetime.now(UTC))
    started = clock.now()
    service.council._clock = clock
    calls = dict.fromkeys(("m1", "m2", "m3"), 0)

    async def member(_intent: Intent, member_id: str) -> IntentEvaluation:
        calls[member_id] += 1
        if calls[member_id] == 1:
            return _approval("deny" if member_id == "m3" else "approve")
        if clock.now() == started:
            clock.advance(seconds=_PAST_FALLBACK_DEADLINE)
        if hold is not None:
            await hold()
        return _approval()

    async def chairman(_intent: Intent) -> ChairmanDecision:
        return ChairmanDecision(threshold="unanimous")

    async def pre_authorize(**_kwargs) -> ChairmanPreAuthDecision:
        return ChairmanPreAuthDecision(authorized=True, threshold="unanimous")

    for member_id in calls:
        _add_member(
            service, member_id, lambda intent, member_id=member_id: member(intent, member_id)
        )
    service._chairman = FakeChairman(chairman, pre_authorize)


def _review_records(service: CouncilService) -> list[LedgerEntry]:
    ledger = service.council.ledger
    return [
        entry
        for sequence in range(ledger.length())
        if (entry := ledger.get(sequence)) is not None
        and entry.entry_type.value == "intent_review_recorded"
    ]


def _debated_authorization() -> AuthorizeInput:
    return AuthorizeInput(
        agent_id="agent-1",
        command="/bin/ls /tmp",
        working_directory="/tmp",
        description="Authorize a review that expires in debate",
        target_resource="/tmp",
        justification="Hold a review across its deadline",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel", [True, False])
async def test_propose_records_a_review_that_expires_during_debate(parallel):
    service = _service(parallel=parallel)
    _debate_outlasts_the_deadline(service)

    result = await service.propose_intent(_proposal("expires in debate"), enable_debate=True)

    assert result.state == "expired" and not result.requires_approval
    assert result.debate_rounds == 1
    workflow = service.council._workflows[result.intent_id]
    assert workflow.state == WorkflowState.EXPIRED
    assert service.council.mint_token(result.intent_id) is None
    reviews = _review_records(service)
    assert len(reviews) == 1
    payload = reviews[0].payload
    assert payload["intent_id"] == result.intent_id
    assert payload["request_type"] == "propose"
    assert payload["final"]["state"] == "expired"
    assert payload["final"]["decision"] == "aborted"
    assert payload["final"]["denial_reason"] == "Review expired before decision"
    assert payload["final"]["approved_at"] is None
    assert len(payload["debate_rounds"]) == 2
    assert reviews[0].sequence in workflow.ledger_sequences
    await service.aclose_workflows()


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel", [True, False])
async def test_authorize_records_a_review_that_expires_during_debate(tmp_path, parallel):
    # A signing key is present, so only the expiry stands between this review and a writ.
    service = _service(parallel=parallel, council_key_path=str(tmp_path))
    _debate_outlasts_the_deadline(service)

    result = await service.authorize(_debated_authorization(), enable_debate=True)

    assert result.status == "denied" and not result.writ
    assert result.debate_rounds == 1
    workflow = service.council._workflows[result.decision_id]
    assert workflow.state == WorkflowState.EXPIRED
    assert "writ_id" not in workflow.metadata
    reviews = _review_records(service)
    assert len(reviews) == 1
    payload = reviews[0].payload
    assert payload["intent_id"] == result.decision_id
    assert payload["request_type"] == "authorize"
    assert payload["final"]["state"] == "expired"
    assert payload["final"]["decision"] == "aborted"
    assert payload["final"]["denial_reason"] == "Review expired before decision"
    assert payload["final"]["approved_at"] is None
    assert len(payload["debate_rounds"]) == 2
    assert reviews[0].sequence in workflow.ledger_sequences
    await service.aclose()


@pytest.mark.asyncio
async def test_cancelled_authorize_records_a_review_that_expires_during_debate(tmp_path):
    service = _service(council_key_path=str(tmp_path))
    debating, release = asyncio.Event(), asyncio.Event()

    async def hold() -> None:
        debating.set()
        await release.wait()

    _debate_outlasts_the_deadline(service, hold)

    pending = asyncio.create_task(service.authorize(_debated_authorization(), enable_debate=True))
    await asyncio.wait_for(debating.wait(), timeout=1)
    pending.cancel()
    await asyncio.sleep(0)
    assert not pending.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending

    (workflow,) = service.council._workflows.values()
    assert workflow.state == WorkflowState.EXPIRED
    assert "writ_id" not in workflow.metadata
    reviews = _review_records(service)
    assert len(reviews) == 1
    assert reviews[0].payload["intent_id"] == workflow.intent_id
    assert reviews[0].payload["final"]["state"] == "expired"
    assert reviews[0].payload["final"]["decision"] == "aborted"
    await service.aclose()


@pytest.mark.asyncio
async def test_cancelled_serial_authorize_records_a_review_that_expired_in_debate(tmp_path):
    # The serial path does not hold a cancelled request until the debate ends,
    # so the review is recorded when the request is cleaned up.
    service = _service(parallel=False, council_key_path=str(tmp_path))
    debating, release = asyncio.Event(), asyncio.Event()

    async def hold() -> None:
        debating.set()
        await release.wait()

    _debate_outlasts_the_deadline(service, hold)

    pending = asyncio.create_task(service.authorize(_debated_authorization(), enable_debate=True))
    await asyncio.wait_for(debating.wait(), timeout=1)
    service.council.maintain_local_workflows()
    (workflow,) = service.council._workflows.values()
    assert workflow.state == WorkflowState.EXPIRED
    pending.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending

    assert workflow.state == WorkflowState.EXPIRED
    assert "writ_id" not in workflow.metadata
    reviews = _review_records(service)
    assert len(reviews) == 1
    payload = reviews[0].payload
    assert payload["intent_id"] == workflow.intent_id
    assert payload["request_type"] == "authorize"
    assert payload["final"]["state"] == "expired"
    assert payload["final"]["decision"] == "aborted"
    assert payload["final"]["denial_reason"] == "Review expired before decision"
    assert reviews[0].sequence in workflow.ledger_sequences
    await service.aclose()


@pytest.mark.asyncio
async def test_review_that_expires_after_its_request_ended_is_recorded_once():
    service = _service()
    clock = MockClock(datetime.now(UTC))
    service.council._clock = clock
    (approved,) = await _approved(service, 1)
    # The request returns with the review still open: one member has not voted.
    (undecided,) = await _awaiting(service, 1)
    assert [entry.payload["intent_id"] for entry in _review_records(service)] == [approved]

    clock.advance(seconds=_PAST_FALLBACK_DEADLINE)
    # The once-a-second maintenance records it, with no request in flight.
    workflow = service.council._workflows[undecided]
    waited_until = time.monotonic() + 10
    while not workflow.metadata.get("audit_committed"):
        assert time.monotonic() < waited_until, "maintenance never recorded the expiry"
        await asyncio.sleep(0.05)
    # One more round: a second pass must not record it again.
    await asyncio.sleep(1.2)

    assert workflow.state == WorkflowState.EXPIRED
    assert service.council._workflows[approved].state == WorkflowState.EXPIRED
    reviews = _review_records(service)
    # The approval keeps the one record its request wrote; the open review gets one.
    assert [entry.payload["final"]["state"] for entry in reviews] == ["approved", "expired"]
    payload = reviews[1].payload
    assert payload["intent_id"] == undecided
    assert payload["request_type"] == "propose"
    assert payload["final"]["decision"] == "aborted"
    assert payload["final"]["denial_reason"] == "Review expired before decision"
    assert reviews[1].sequence in workflow.ledger_sequences
    assert workflow.metadata["audit_committed"] is True
    await service.aclose_workflows()


@pytest.mark.asyncio
async def test_expired_review_whose_record_fails_is_recorded_on_the_next_pass(caplog):
    service = _service()
    clock = MockClock(datetime.now(UTC))
    service.council._clock = clock
    (undecided,) = await _awaiting(service, 1)
    service._workflow_janitor.cancel()  # this test runs the maintenance step itself
    append = service._append_review_entry
    outage = [RuntimeError("ledger unavailable")]

    def append_after_the_outage(payload, intent_id):
        if outage:
            raise outage.pop()
        return append(payload, intent_id)

    service._append_review_entry = append_after_the_outage  # type: ignore[method-assign]
    clock.advance(seconds=_PAST_FALLBACK_DEADLINE)

    with caplog.at_level(logging.ERROR, logger="nomos.nat.functions"):
        await service._record_expired_reviews()
    assert "Expired review audit failed" in caplog.text
    assert _review_records(service) == []
    assert undecided in service._undecided_reviews

    await service._record_expired_reviews()
    await service._record_expired_reviews()
    reviews = _review_records(service)
    assert [entry.payload["final"]["state"] for entry in reviews] == ["expired"]
    assert reviews[0].payload["intent_id"] == undecided
    assert service._undecided_reviews == {}
    await service.aclose_workflows()


def _replica(dsn: str) -> CouncilService:
    """One of several services sharing a ledger and a workflow store."""
    return CouncilService(
        CouncilSettings(
            council_id="shared-store",
            require_auth=False,
            require_justification=True,
            llm_backends={},
            members=[],
            chairman=ChairmanConfig(),
            opa=OPAConfig(enabled=False),
            council_key_path="/nonexistent/nomos-decision-deadline-tests/keys",
            storage={"mode": "postgres", "dsn": dsn},
        )
    )


_PG_SKIP = postgres_unavailable_reason()


@pytest.mark.skipif(_PG_SKIP is not None, reason=_PG_SKIP or "")
@pytest.mark.asyncio
@pytest.mark.parametrize("decided_elsewhere", [None, WorkflowState.APPROVED, WorkflowState.DENIED])
async def test_expired_review_is_recorded_only_if_the_shared_store_has_no_decision(
    caplog, decided_elsewhere
):
    dsn = postgres_dsn_for_tests()
    ensure_migrated_and_clean(dsn)
    holder, other = _replica(dsn), _replica(dsn)
    clock = MockClock(datetime.now(UTC))
    holder.council._clock = clock
    (undecided,) = await _awaiting(holder, 1)
    holder._workflow_janitor.cancel()  # this test runs the maintenance step itself
    expected = [("expired", "aborted")]
    if decided_elsewhere is not None:
        # No request of the service decides a review another replica holds, so
        # the other replica is handed the stored review and its decision here.
        decided = IntentWorkflow.from_durable_dict(other._workflow_store.get_workflow(undecided))
        other.council._workflows[undecided] = decided
        decided.transition_to(decided_elsewhere)
        decision = "approve" if decided_elsewhere == WorkflowState.APPROVED else "deny"
        await other._record_consolidated_review(
            decided.intent,
            request_type="propose",
            final_state=decided_elsewhere.value,
            decision=decision,
            workflow=decided,
        )
        await other._workflow_runtime.flush(undecided, decided.logical_version)
        expected = [(decided_elsewhere.value, decision)]

    clock.advance(seconds=_PAST_FALLBACK_DEADLINE)
    with caplog.at_level(logging.ERROR, logger="nomos.nat.functions"):
        await holder._record_expired_reviews()
    workflow = holder.council._workflows[undecided]
    await holder._workflow_runtime.flush(undecided, workflow.logical_version)

    assert [
        (entry.payload["final"]["state"], entry.payload["final"]["decision"])
        for entry in _review_records(holder)
    ] == expected
    assert "Expired review audit failed" not in caplog.text
    assert holder._workflow_store.get_workflow(undecided)["state"] == expected[0][0]
    await holder.aclose()
    await other.aclose()


@pytest.mark.skipif(_PG_SKIP is not None, reason=_PG_SKIP or "")
@pytest.mark.asyncio
async def test_expiry_record_refused_for_a_review_decided_elsewhere_is_not_retried(caplog):
    """With the store unreadable, the ledger's refusal is what ends the attempts."""
    dsn = postgres_dsn_for_tests()
    ensure_migrated_and_clean(dsn)
    holder, other = _replica(dsn), _replica(dsn)
    clock = MockClock(datetime.now(UTC))
    holder.council._clock = clock
    (undecided,) = await _awaiting(holder, 1)
    holder._workflow_janitor.cancel()  # this test runs the maintenance step itself
    decided = IntentWorkflow.from_durable_dict(other._workflow_store.get_workflow(undecided))
    other.council._workflows[undecided] = decided
    decided.transition_to(WorkflowState.DENIED)
    await other._record_consolidated_review(
        decided.intent,
        request_type="propose",
        final_state="denied",
        decision="deny",
        workflow=decided,
    )
    await other._workflow_runtime.flush(undecided, decided.logical_version)

    async def unreadable(*_args, **_kwargs):
        raise RuntimeError("store unavailable")

    holder._workflow_runtime.read = unreadable  # type: ignore[method-assign]
    clock.advance(seconds=_PAST_FALLBACK_DEADLINE)
    with caplog.at_level(logging.ERROR, logger="nomos.nat.functions"):
        await holder._record_expired_reviews()

    assert "Expired review audit failed" in caplog.text
    assert holder._undecided_reviews == {}
    assert [entry.payload["final"]["state"] for entry in _review_records(holder)] == ["denied"]
    await holder.aclose()
    await other.aclose()
