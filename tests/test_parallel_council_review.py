# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

"""Concurrency, cancellation, threshold, and telemetry tests for initial review."""

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from nomos.auth.authenticator import AgentIdentity
from nomos.auth.permissions import PermissionScope
from nomos.auth.writ_factory import WritFactory
from nomos.config.settings import ChairmanConfig, CouncilSettings, OPAConfig
from nomos.level_0.types import CouncilMember, Intent
from nomos.level_1.policy import DecisionType, PolicyRule
from nomos.llm.council_agent import (
    ChairmanDecision,
    ChairmanPreAuthDecision,
    IntentEvaluation,
)
from nomos.nat.functions import AuthorizeInput, CouncilService, ProposeIntentInput


class FakeMember:
    def __init__(
        self,
        member_id: str,
        evaluate: Callable[[Intent], Awaitable[IntentEvaluation]],
    ):
        self.member_id = member_id
        self.display_name = member_id
        self.provider_name = "fake"
        self._evaluate = evaluate

    async def evaluate_intent(self, intent: Intent) -> IntentEvaluation:
        return await self._evaluate(intent)


class FakeChairman:
    provider_name = "fake"

    def __init__(
        self,
        decide: Callable[[Intent], Awaitable[ChairmanDecision]],
        pre_authorize: Callable[..., Awaitable[ChairmanPreAuthDecision]] | None = None,
    ):
        self._decide = decide
        self._pre_authorize = pre_authorize

    async def decide_threshold(self, intent: Intent) -> ChairmanDecision:
        return await self._decide(intent)

    async def pre_authorize(self, **kwargs: Any) -> ChairmanPreAuthDecision:
        assert self._pre_authorize is not None
        return await self._pre_authorize(**kwargs)


def _service(*, parallel: bool = True, council_key_path: str = "/app/keys") -> CouncilService:
    return CouncilService(
        CouncilSettings(
            council_id="parallel-test",
            require_auth=False,
            require_justification=True,
            llm_backends={},
            members=[],
            chairman=ChairmanConfig(parallel_member_evaluation=parallel),
            opa=OPAConfig(enabled=False),
            council_key_path=council_key_path,
        )
    )


def _add_member(
    service: CouncilService,
    member_id: str,
    evaluate: Callable[[Intent], Awaitable[IntentEvaluation]],
) -> None:
    service._agents.append(FakeMember(member_id, evaluate))  # type: ignore[arg-type]
    service.council.register_council_member(
        CouncilMember(
            member_id=member_id,
            public_key_hex="",
            display_name=member_id,
            weight=1,
        )
    )


def _proposal(tag: str = "parallel") -> ProposeIntentInput:
    return ProposeIntentInput(
        agent_id="agent-1",
        command="/bin/ls /tmp",
        working_directory="/tmp",
        description=f"Inspect temporary state for {tag} review",
        target_resource="/tmp",
        justification="Validate the parallel council review path",
    )


def _approval(decision: str = "approve") -> IntentEvaluation:
    return IntentEvaluation(decision=decision, reasoning=f"member says {decision}")


@pytest.mark.asyncio
async def test_parallel_happy_path_enters_chairman_and_all_members_before_release():
    service = _service()
    entered: set[str] = set()
    all_entered = asyncio.Event()
    release = asyncio.Event()
    received: list[Intent] = []

    async def wait_at_latch(name: str, intent: Intent) -> IntentEvaluation:
        entered.add(name)
        received.append(intent)
        if len(entered) == 4:
            all_entered.set()
        await release.wait()
        return _approval()

    async def chairman(_intent: Intent) -> ChairmanDecision:
        entered.add("chairman")
        if len(entered) == 4:
            all_entered.set()
        await release.wait()
        return ChairmanDecision(threshold="unanimous", reasoning="all members required")

    for member_id in ("m1", "m2", "m3"):
        _add_member(
            service,
            member_id,
            lambda intent, member_id=member_id: wait_at_latch(member_id, intent),
        )
    service._chairman = FakeChairman(chairman)  # type: ignore[assignment]

    pending = asyncio.create_task(service.propose_intent(_proposal("latch")))
    await asyncio.wait_for(all_entered.wait(), timeout=1)

    assert entered == {"chairman", "m1", "m2", "m3"}
    assert len({id(intent) for intent in received}) == 1
    assert service.council.get_active_workflows() == []

    release.set()
    result = await pending
    assert result.state == "approved"
    assert [vote.member_id for vote in result.council_members] == ["m1", "m2", "m3"]


@pytest.mark.asyncio
async def test_code_review_finishes_before_parallel_fan_out(monkeypatch):
    service = _service()
    code_review_finished = False

    async def code_review(intent: Intent):
        nonlocal code_review_finished
        await asyncio.sleep(0)
        code_review_finished = True
        return intent, None

    async def chairman(_intent: Intent) -> ChairmanDecision:
        assert code_review_finished
        return ChairmanDecision(threshold="unanimous")

    async def member(_intent: Intent) -> IntentEvaluation:
        assert code_review_finished
        return _approval()

    monkeypatch.setattr(service, "_run_code_review", code_review)
    for member_id in ("m1", "m2", "m3"):
        _add_member(service, member_id, member)
    service._chairman = FakeChairman(chairman)  # type: ignore[assignment]

    result = await service.propose_intent(_proposal("code review ordering"))
    assert result.state == "approved"


@pytest.mark.asyncio
async def test_legacy_switch_preserves_chairman_submit_member_order():
    service = _service(parallel=False)
    order: list[str] = []

    async def chairman(_intent: Intent) -> ChairmanDecision:
        order.append("chairman")
        return ChairmanDecision(threshold="unanimous")

    async def member(_intent: Intent) -> IntentEvaluation:
        order.append("member")
        return _approval()

    for member_id in ("m1", "m2", "m3"):
        _add_member(service, member_id, member)
    service._chairman = FakeChairman(chairman)  # type: ignore[assignment]
    original_submit = service.council.submit_intent

    def recording_submit(*args: Any, **kwargs: Any):
        order.append("submit")
        return original_submit(*args, **kwargs)

    service.council.submit_intent = recording_submit  # type: ignore[method-assign]
    result = await service.propose_intent(_proposal("legacy"))

    assert result.state == "approved"
    assert order[0:2] == ["chairman", "submit"]
    assert order[2:] == ["member", "member", "member"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("threshold", "decisions", "expected"),
    [
        ("quorum", ["approve", "approve", "deny"], "approved"),
        ("unanimous", ["approve", "approve", "deny"], "denied"),
    ],
)
async def test_parallel_commit_applies_chairman_threshold_before_tally(
    threshold: str,
    decisions: list[str],
    expected: str,
):
    service = _service()

    async def chairman(_intent: Intent) -> ChairmanDecision:
        return ChairmanDecision(threshold=threshold)

    for index, decision in enumerate(decisions):

        async def evaluate(_intent: Intent, decision: str = decision) -> IntentEvaluation:
            return _approval(decision)

        _add_member(service, f"m{index}", evaluate)
    service._chairman = FakeChairman(chairman)  # type: ignore[assignment]

    result = await service.propose_intent(_proposal(threshold))
    workflow = service.council.get_workflow(result.intent_id)

    assert result.state == expected
    assert workflow is not None and workflow.vote_record is not None
    assert workflow.metadata["review_threshold"] == threshold
    assert workflow.vote_record.required_weight == (2 if threshold == "quorum" else 3)


@pytest.mark.asyncio
async def test_chairman_denial_cancels_members_and_discards_completed_results(monkeypatch):
    service = _service()
    entered: set[str] = set()
    all_entered = asyncio.Event()
    cancelled: set[str] = set()

    async def member(_intent: Intent, member_id: str) -> IntentEvaluation:
        entered.add(member_id)
        if len(entered) == 3:
            all_entered.set()
        if member_id == "m1":
            return _approval()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.add(member_id)
            raise

    for member_id in ("m1", "m2", "m3"):
        _add_member(
            service,
            member_id,
            lambda intent, member_id=member_id: member(intent, member_id),
        )

    async def deny(**_kwargs: Any) -> ChairmanPreAuthDecision:
        await all_entered.wait()
        return ChairmanPreAuthDecision(
            authorized=False,
            reasoning="permission gate denied",
            missing_permissions=["shell.execute"],
        )

    async def unused(_intent: Intent) -> ChairmanDecision:
        raise AssertionError("authenticated authorize must use pre_authorize")

    service._chairman = FakeChairman(unused, deny)  # type: ignore[assignment]
    identity = AgentIdentity(
        agent_id="agent-1",
        display_name="Agent",
        permissions={permission.value for permission in PermissionScope},
        councils={service.council_id},
    )

    async def resolve(*_args: Any, **_kwargs: Any):
        return identity, ""

    monkeypatch.setattr(service, "_resolve_request_agent", resolve)
    result = await service.authorize(
        AuthorizeInput(
            auth_token="token",
            agent_id="agent-1",
            command="/bin/ls /tmp",
            working_directory="/tmp",
            description="Inspect temporary state before an authorized action",
            target_resource="/tmp",
            justification="Test chairman denial cleanup",
        )
    )

    assert result.status == "denied"
    assert cancelled == {"m2", "m3"}
    assert service.council.get_workflow(result.decision_id) is None
    assert service.council._submission_preflights == {}


@pytest.mark.asyncio
async def test_unexpected_chairman_exception_is_sanitized_and_drains_members():
    service = _service()
    members_entered = asyncio.Event()
    entered = 0
    cancelled: set[str] = set()

    async def member(_intent: Intent, member_id: str) -> IntentEvaluation:
        nonlocal entered
        entered += 1
        if entered == 3:
            members_entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.add(member_id)
            raise

    for member_id in ("m1", "m2", "m3"):
        _add_member(
            service,
            member_id,
            lambda intent, member_id=member_id: member(intent, member_id),
        )

    async def chairman(_intent: Intent) -> ChairmanDecision:
        await members_entered.wait()
        raise RuntimeError("provider secret must not escape")

    service._chairman = FakeChairman(chairman)  # type: ignore[assignment]
    result = await service.propose_intent(_proposal("chairman exception"))

    assert not result.success
    assert result.error == "Chairman evaluation failed"
    assert "provider secret" not in result.error
    assert cancelled == {"m1", "m2", "m3"}
    assert service.council.get_workflow(result.intent_id) is None
    assert service.council._submission_preflights == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("chairman_outcome", ["deny", "error"])
async def test_late_chairman_exit_reports_completed_members_as_discarded(chairman_outcome):
    service = _service()
    members_finished = asyncio.Event()
    finished = 0

    async def member(_intent: Intent) -> IntentEvaluation:
        nonlocal finished
        finished += 1
        if finished == 3:
            members_finished.set()
        return _approval()

    for member_id in ("m1", "m2", "m3"):
        _add_member(service, member_id, member)

    async def chairman_call() -> ChairmanPreAuthDecision:
        await members_finished.wait()
        await asyncio.sleep(0.05)
        if chairman_outcome == "error":
            raise RuntimeError("late chairman failure")
        return ChairmanPreAuthDecision(authorized=False, reasoning="late denial")

    intent = Intent.create(
        agent_id="agent-1",
        action_type="system.noop",
        description=f"Verify completed member telemetry after chairman {chairman_outcome}",
        target_resource="/tmp/telemetry",
        justification="Keep overlapping stage telemetry truthful",
    )
    stages: dict[str, float] = {}
    intervals: list[dict[str, Any]] = []
    actors: list[dict[str, Any]] = []
    started = time.perf_counter()
    pending = service._run_parallel_initial_calls(
        intent,
        chairman_call,
        lambda decision: decision.authorized,
        total_start=started,
        stages=stages,
        stage_intervals=intervals,
        actors=actors,
    )

    if chairman_outcome == "error":
        with pytest.raises(RuntimeError, match="late chairman failure"):
            await pending
    else:
        decision, evaluations = await pending
        assert decision.authorized is False
        assert evaluations is None

    member_interval = next(
        interval for interval in intervals if interval["name"] == "member_evaluation"
    )
    member_actors = [actor for actor in actors if actor["role"] == "council_member"]
    max_actor_ms = max(actor["duration_ms"] for actor in member_actors)

    assert len(member_actors) == 3
    assert member_interval["status"] == "discarded"
    assert member_interval["duration_ms"] == pytest.approx(stages["member_evaluation"], abs=0.1)
    assert max_actor_ms <= member_interval["duration_ms"] < max_actor_ms + 20
    assert member_interval["duration_ms"] + 30 < stages["chairman"]


@pytest.mark.asyncio
async def test_member_exception_becomes_one_deny_vote_and_siblings_are_tallied():
    service = _service()

    async def chairman(_intent: Intent) -> ChairmanDecision:
        return ChairmanDecision(threshold="quorum")

    async def raises(_intent: Intent) -> IntentEvaluation:
        raise RuntimeError("member unavailable")

    _add_member(service, "m1", lambda _intent: asyncio.sleep(0, result=_approval()))
    _add_member(service, "m2", raises)
    _add_member(service, "m3", lambda _intent: asyncio.sleep(0, result=_approval()))
    service._chairman = FakeChairman(chairman)  # type: ignore[assignment]

    result = await service.propose_intent(_proposal("member exception"))
    by_member = {vote.member_id: vote for vote in result.council_members}

    assert result.state == "approved"
    assert by_member["m2"].decision == "deny"
    assert "Evaluation error: member unavailable" in by_member["m2"].reasoning
    assert by_member["m1"].decision == by_member["m3"].decision == "approve"


@pytest.mark.asyncio
async def test_invalid_member_result_fails_closed_without_orphaning_siblings():
    service = _service()

    async def chairman(_intent: Intent) -> ChairmanDecision:
        return ChairmanDecision(threshold="quorum")

    async def invalid(_intent: Intent) -> Any:
        return None

    _add_member(service, "m1", invalid)
    _add_member(service, "m2", lambda _intent: asyncio.sleep(0.01, result=_approval()))
    _add_member(service, "m3", lambda _intent: asyncio.sleep(0.01, result=_approval()))
    service._chairman = FakeChairman(chairman)  # type: ignore[assignment]

    result = await service.propose_intent(_proposal("invalid member result"))
    by_member = {vote.member_id: vote for vote in result.council_members}

    assert result.state == "approved"
    assert by_member["m1"].decision == "deny"
    assert "Evaluation error" in by_member["m1"].reasoning
    assert by_member["m2"].decision == by_member["m3"].decision == "approve"
    assert not [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and not task.done()
        and "_evaluate_agents.<locals>._evaluate_one" in task.get_coro().__qualname__
    ]


@pytest.mark.asyncio
async def test_policy_deny_short_circuits_parallel_calls_and_keeps_audit_stable():
    calls: dict[bool, list[str]] = {False: [], True: []}
    payloads: dict[bool, dict[str, Any]] = {}
    ledger_types: dict[bool, list[str]] = {}

    for parallel in (False, True):
        service = _service(parallel=parallel)

        async def chairman(_intent: Intent, *, parallel: bool = parallel) -> ChairmanDecision:
            calls[parallel].append("chairman")
            return ChairmanDecision(
                threshold="quorum",
                reasoning="unused after deterministic policy denial",
                risk_level="high",
            )

        async def member(_intent: Intent, *, parallel: bool = parallel) -> IntentEvaluation:
            calls[parallel].append("member")
            return _approval()

        for member_id in ("m1", "m2", "m3"):
            _add_member(service, member_id, member)
        service._chairman = FakeChairman(chairman)  # type: ignore[assignment]
        service.council._policy_engine.add_rule(
            PolicyRule(
                rule_id="deny-parallel-test",
                name="Deny parallel test commands",
                priority=0,
                action_types={"system.exec"},
                decision=DecisionType.DENY,
            )
        )

        result = await service.propose_intent(_proposal(f"policy deny {parallel}"))
        assert not result.success
        assert result.state == "denied"
        assert service.council._submission_preflights == {}
        review_entries = [
            entry
            for sequence in range(service.council.ledger.length())
            if (entry := service.council.ledger.get(sequence)) is not None
            and entry.entry_type.value == "intent_review_recorded"
        ]
        assert len(review_entries) == 1
        payloads[parallel] = review_entries[0].payload
        ledger_types[parallel] = [
            entry.entry_type.value
            for sequence in range(service.council.ledger.length())
            if (entry := service.council.ledger.get(sequence)) is not None
        ]

    assert calls[False] == ["chairman"]
    assert calls[True] == []
    assert ledger_types[False] == ledger_types[True]
    for key in ("policy_evaluation", "quorum"):
        assert payloads[False][key] == payloads[True][key]
    for key in ("state", "decision", "denial_reason"):
        assert payloads[False]["final"][key] == payloads[True]["final"][key]
    assert payloads[False]["chairman"] == payloads[True]["chairman"] == {}


@pytest.mark.asyncio
async def test_authorize_policy_deny_has_zero_llm_calls_and_audit_inclusive_total(monkeypatch):
    service = _service()
    calls: list[str] = []
    records: list[dict[str, Any]] = []
    service._telemetry.publish_later = records.append

    async def chairman(_intent: Intent) -> ChairmanDecision:
        calls.append("chairman")
        return ChairmanDecision(threshold="unanimous")

    async def member(_intent: Intent) -> IntentEvaluation:
        calls.append("member")
        return _approval()

    for member_id in ("m1", "m2", "m3"):
        _add_member(service, member_id, member)
    service._chairman = FakeChairman(chairman)  # type: ignore[assignment]
    service.council._policy_engine.add_rule(
        PolicyRule(
            rule_id="deny-parallel-authorize",
            name="Deny parallel authorize commands",
            priority=0,
            action_types={"system.exec"},
            decision=DecisionType.DENY,
        )
    )
    original_record = service._record_consolidated_review

    def delayed_record(*args: Any, **kwargs: Any) -> None:
        time.sleep(0.05)
        original_record(*args, **kwargs)

    monkeypatch.setattr(service, "_record_consolidated_review", delayed_record)
    started = time.perf_counter()
    result = await service.authorize(
        AuthorizeInput(
            agent_id="agent-1",
            command="/bin/ls /tmp",
            working_directory="/tmp",
            description="Inspect temporary state for policy denial",
            target_resource="/tmp",
            justification="Verify authorize short-circuit telemetry",
        )
    )
    wall_ms = (time.perf_counter() - started) * 1000

    assert result.status == "denied"
    assert calls == []
    assert service.council._submission_preflights == {}
    assert records[-1]["stages"]["total"] >= 50
    assert abs(wall_ms - records[-1]["stages"]["total"]) < 20


@pytest.mark.asyncio
async def test_caller_cancellation_drains_children_and_releases_reservation():
    service = _service()
    all_entered = asyncio.Event()
    entered = 0
    cancelled: set[str] = set()

    async def wait_forever(name: str) -> None:
        nonlocal entered
        entered += 1
        if entered == 4:
            all_entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.add(name)
            raise

    async def chairman(_intent: Intent) -> ChairmanDecision:
        await wait_forever("chairman")
        raise AssertionError("unreachable")

    async def member(_intent: Intent, member_id: str) -> IntentEvaluation:
        await wait_forever(member_id)
        raise AssertionError("unreachable")

    for member_id in ("m1", "m2", "m3"):
        _add_member(
            service,
            member_id,
            lambda intent, member_id=member_id: member(intent, member_id),
        )
    service._chairman = FakeChairman(chairman)  # type: ignore[assignment]

    pending = asyncio.create_task(service.propose_intent(_proposal("caller cancellation")))
    await asyncio.wait_for(all_entered.wait(), timeout=1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    assert cancelled == {"chairman", "m1", "m2", "m3"}
    assert service.council._submission_preflights == {}
    assert service.council.get_active_workflows() == []


@pytest.mark.asyncio
async def test_concurrent_preflights_cannot_race_past_pending_cap():
    service = _service()
    service.council.config.max_pending_intents = 1
    first = Intent.create(
        agent_id="agent-1",
        action_type="system.noop",
        description="First concurrent preflight reservation",
        target_resource="/tmp/first",
        justification="Verify serialized admission",
    )
    second = Intent.create(
        agent_id="agent-1",
        action_type="system.noop",
        description="Second concurrent preflight reservation",
        target_resource="/tmp/second",
        justification="Verify serialized admission",
    )

    results = await asyncio.gather(
        service._preflight_intent(first),
        service._preflight_intent(second),
    )

    assert sum(result.success for result in results) == 1
    assert sum(result.error == "Maximum pending intents exceeded" for result in results) == 1
    admitted = next(result.preflight for result in results if result.success)
    assert admitted is not None
    await service._release_preflight_safely(admitted)


@pytest.mark.asyncio
async def test_concurrent_commits_keep_legacy_ledger_groups_serialized():
    service = _service()
    service.council.config.consolidated_review_audit = False
    service.council._quorum_manager._record_audit_entries = True
    entered = 0
    both_reviews_ready = asyncio.Event()
    release = asyncio.Event()

    async def wait_for_both_reviews(_intent: Intent) -> IntentEvaluation:
        nonlocal entered
        entered += 1
        if entered == 8:  # two chairmen plus three members per request
            both_reviews_ready.set()
        await release.wait()
        return _approval()

    async def chairman(intent: Intent) -> ChairmanDecision:
        await wait_for_both_reviews(intent)
        return ChairmanDecision(threshold="unanimous")

    for member_id in ("m1", "m2", "m3"):
        _add_member(service, member_id, wait_for_both_reviews)
    service._chairman = FakeChairman(chairman)  # type: ignore[assignment]

    requests = [
        asyncio.create_task(service.propose_intent(_proposal("concurrent-a"))),
        asyncio.create_task(service.propose_intent(_proposal("concurrent-b"))),
    ]
    await asyncio.wait_for(both_reviews_ready.wait(), timeout=1)
    release.set()
    results = await asyncio.gather(*requests)
    assert all(result.state == "approved" for result in results)

    entries = [
        entry
        for sequence in range(1, service.council.ledger.length())
        if (entry := service.council.ledger.get(sequence)) is not None
    ]
    expected_types = [
        "intent_submitted",
        "intent_evaluated",
        "quorum_vote",
        "quorum_vote",
        "quorum_vote",
        "quorum_vote",
        "decision_made",
    ]
    assert [entry.entry_type.value for entry in entries] == expected_types * 2
    for offset in (0, len(expected_types)):
        group = entries[offset : offset + len(expected_types)]
        intent_ids = {entry.payload["intent_id"] for entry in group}
        assert len(intent_ids) == 1
        assert group[2].payload["action"] == "opened"
        assert [entry.payload["voter_id"] for entry in group[3:6]] == ["m1", "m2", "m3"]


@pytest.mark.asyncio
async def test_parallel_authorize_records_one_review_before_writ_mint(tmp_path, monkeypatch):
    service = _service(council_key_path=str(tmp_path))
    review_existed_at_mint = False
    original_mint_writ = WritFactory.mint_writ

    async def mint_after_review(factory: WritFactory, *args: Any, **kwargs: Any):
        nonlocal review_existed_at_mint
        review_existed_at_mint = (
            sum(
                entry is not None and entry.entry_type.value == "intent_review_recorded"
                for sequence in range(service.council.ledger.length())
                if (entry := service.council.ledger.get(sequence)) is not None
            )
            == 1
        )
        return await original_mint_writ(factory, *args, **kwargs)

    monkeypatch.setattr(WritFactory, "mint_writ", mint_after_review)

    async def chairman(_intent: Intent) -> ChairmanDecision:
        return ChairmanDecision(
            threshold="unanimous",
            reasoning="Require every member",
            risk_level="high",
        )

    async def member(_intent: Intent) -> IntentEvaluation:
        return _approval()

    for member_id in ("m1", "m2", "m3"):
        _add_member(service, member_id, member)
    service._chairman = FakeChairman(chairman)  # type: ignore[assignment]

    result = await service.authorize(
        AuthorizeInput(
            agent_id="agent-1",
            command="/bin/ls /tmp",
            working_directory="/tmp",
            description="Inspect temporary state before minting a writ",
            target_resource="/tmp",
            justification="Verify review audit ordering",
        )
    )

    assert result.status == "approved", (result.error, result.guidance)
    entries = [
        entry
        for sequence in range(service.council.ledger.length())
        if (entry := service.council.ledger.get(sequence)) is not None
    ]
    review_indexes = [
        index
        for index, entry in enumerate(entries)
        if entry.entry_type.value == "intent_review_recorded"
    ]
    assert len(review_indexes) == 1
    assert review_existed_at_mint is True
    review = entries[review_indexes[0]].payload
    assert review["chairman"]["threshold"] == "unanimous"
    assert review["policy_evaluation"]["decision"] == "require_approval"
    assert review["quorum"]["required_weight"] == 3
    assert [vote["member_id"] for vote in review["quorum"]["member_votes"]] == [
        "m1",
        "m2",
        "m3",
    ]


@pytest.mark.asyncio
async def test_cancellation_during_preflight_releases_created_reservation():
    service = _service()
    intent = Intent.create(
        agent_id="agent-1",
        action_type="system.noop",
        description="Cancel while the policy preflight runs",
        target_resource="/tmp/cancel-preflight",
        justification="Verify cancellation-safe reservation cleanup",
    )
    preflight_entered = threading.Event()
    release_preflight = threading.Event()
    original_preflight = service.council.preflight_intent

    def blocking_preflight(*args: Any, **kwargs: Any):
        preflight_entered.set()
        assert release_preflight.wait(timeout=1)
        return original_preflight(*args, **kwargs)

    service.council.preflight_intent = blocking_preflight  # type: ignore[method-assign]
    pending = asyncio.create_task(service._preflight_intent(intent))
    assert await asyncio.to_thread(preflight_entered.wait, 1)
    pending.cancel()
    release_preflight.set()

    with pytest.raises(asyncio.CancelledError):
        await pending

    assert service.council._submission_preflights == {}


@pytest.mark.asyncio
async def test_cancellation_during_commit_finishes_votes_and_final_audit():
    service = _service()

    async def chairman(_intent: Intent) -> ChairmanDecision:
        return ChairmanDecision(threshold="unanimous")

    async def member(_intent: Intent) -> IntentEvaluation:
        return _approval()

    for member_id in ("m1", "m2", "m3"):
        _add_member(service, member_id, member)
    service._chairman = FakeChairman(chairman)  # type: ignore[assignment]

    submit_entered = threading.Event()
    release_submit = threading.Event()
    original_submit = service.council.submit_intent

    def blocking_submit(*args: Any, **kwargs: Any):
        submit_entered.set()
        assert release_submit.wait(timeout=1)
        return original_submit(*args, **kwargs)

    service.council.submit_intent = blocking_submit  # type: ignore[method-assign]
    pending = asyncio.create_task(service.propose_intent(_proposal("commit cancellation")))
    assert await asyncio.to_thread(submit_entered.wait, 1)
    pending.cancel()
    release_submit.set()

    with pytest.raises(asyncio.CancelledError):
        await pending

    workflows = list(service.council._workflows.values())
    assert len(workflows) == 1
    workflow = workflows[0]
    assert workflow.state.value == "approved"
    assert workflow.vote_record is not None
    assert list(workflow.vote_record.votes) == ["m1", "m2", "m3"]
    review_entries = [
        entry
        for sequence in range(service.council.ledger.length())
        if (entry := service.council.ledger.get(sequence)) is not None
        and entry.entry_type.value == "intent_review_recorded"
    ]
    assert len(review_entries) == 1
    assert service.council._submission_preflights == {}


@pytest.mark.asyncio
async def test_parallel_latency_and_interval_telemetry_reflect_overlap():
    service = _service()
    records: list[dict[str, Any]] = []
    service._telemetry.publish_later = records.append

    async def chairman(_intent: Intent) -> ChairmanDecision:
        await asyncio.sleep(0.06)
        return ChairmanDecision(threshold="unanimous")

    async def member(_intent: Intent) -> IntentEvaluation:
        await asyncio.sleep(0.06)
        return _approval()

    for member_id in ("m1", "m2", "m3"):
        _add_member(service, member_id, member)
    service._chairman = FakeChairman(chairman)  # type: ignore[assignment]

    started = time.perf_counter()
    result = await service.propose_intent(_proposal("telemetry"))
    elapsed = time.perf_counter() - started
    payload = records[-1]

    assert result.state == "approved"
    assert elapsed < 0.11
    assert payload["timing_model"] == "overlapping"
    intervals = {interval["name"]: interval for interval in payload["stage_intervals"]}
    assert intervals["chairman"]["overlap_group"] == "initial_review"
    assert intervals["member_evaluation"]["overlap_group"] == "initial_review"
    assert payload["stages"]["parallel_review_wall"] < (
        payload["stages"]["chairman"] + payload["stages"]["member_evaluation"]
    )
    assert payload["stages"]["total"] >= payload["stages"]["parallel_review_wall"]
    review = next(
        entry
        for sequence in range(service.council.ledger.length())
        if (entry := service.council.ledger.get(sequence)) is not None
        and entry.entry_type.value == "intent_review_recorded"
    )
    assert review.payload["timing"]["timing_model"] == "overlapping"
    assert review.payload["timing"]["stage_intervals"] == payload["stage_intervals"]


@pytest.mark.asyncio
async def test_parallel_total_includes_delayed_final_audit(monkeypatch):
    service = _service()
    records: list[dict[str, Any]] = []
    service._telemetry.publish_later = records.append

    async def chairman(_intent: Intent) -> ChairmanDecision:
        return ChairmanDecision(threshold="unanimous")

    async def member(_intent: Intent) -> IntentEvaluation:
        return _approval()

    for member_id in ("m1", "m2", "m3"):
        _add_member(service, member_id, member)
    service._chairman = FakeChairman(chairman)  # type: ignore[assignment]
    original_record = service._record_consolidated_review

    def delayed_record(*args: Any, **kwargs: Any) -> None:
        time.sleep(0.05)
        original_record(*args, **kwargs)

    monkeypatch.setattr(service, "_record_consolidated_review", delayed_record)
    started = time.perf_counter()
    result = await service.propose_intent(_proposal("delayed audit telemetry"))
    wall_ms = (time.perf_counter() - started) * 1000

    assert result.state == "approved"
    reported_total_ms = records[-1]["stages"]["total"]
    assert reported_total_ms >= 50
    assert abs(wall_ms - reported_total_ms) < 20


@pytest.mark.asyncio
async def test_parallel_review_collect_excludes_submit_and_tally_time(monkeypatch):
    service = _service()
    records: list[dict[str, Any]] = []
    service._telemetry.publish_later = records.append

    async def chairman(_intent: Intent) -> ChairmanDecision:
        await asyncio.sleep(0.02)
        return ChairmanDecision(threshold="unanimous")

    async def member(_intent: Intent) -> IntentEvaluation:
        await asyncio.sleep(0.02)
        return _approval()

    for member_id in ("m1", "m2", "m3"):
        _add_member(service, member_id, member)
    service._chairman = FakeChairman(chairman)  # type: ignore[assignment]
    original_submit = service.council.submit_intent

    def delayed_submit(*args: Any, **kwargs: Any):
        time.sleep(0.05)
        return original_submit(*args, **kwargs)

    monkeypatch.setattr(service.council, "submit_intent", delayed_submit)
    result = await service.propose_intent(_proposal("review stage boundary"))
    stages = records[-1]["stages"]

    assert result.state == "approved"
    assert stages["submit"] >= 50
    assert stages["review_collect"] < 40
    assert stages["review_collect"] >= stages["member_evaluation"]


@pytest.mark.asyncio
async def test_parallel_latency_is_max_while_legacy_latency_is_sum():
    async def run(parallel: bool) -> float:
        service = _service(parallel=parallel)

        async def chairman(_intent: Intent) -> ChairmanDecision:
            await asyncio.sleep(0.05)
            return ChairmanDecision(threshold="unanimous")

        async def member(_intent: Intent) -> IntentEvaluation:
            await asyncio.sleep(0.05)
            return _approval()

        for member_id in ("m1", "m2", "m3"):
            _add_member(service, member_id, member)
        service._chairman = FakeChairman(chairman)  # type: ignore[assignment]
        started = time.perf_counter()
        result = await service.propose_intent(_proposal(f"latency-{parallel}"))
        assert result.state == "approved"
        return time.perf_counter() - started

    parallel_elapsed = await run(True)
    legacy_elapsed = await run(False)

    assert parallel_elapsed < 0.09
    assert legacy_elapsed >= 0.095
    assert parallel_elapsed + 0.03 < legacy_elapsed
