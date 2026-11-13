# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""I/O-boundary cancellation and bounded state retention regressions."""

import asyncio
import threading
from datetime import UTC, datetime, timedelta

import pytest

from nomos.level_0.clock import MockClock
from nomos.level_0.types import Intent
from nomos.level_0.types.workflow_snapshot import WorkflowSnapshot
from nomos.nat.functions import IntentStatusInput
from nomos.nat.workflow_runtime import WorkflowRuntime
from tests.test_parallel_council_review import _proposal, _service


class Store:
    def __init__(self):
        self.rows = {}
        self.versions = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def upsert_snapshot(self, snapshot):
        self.entered.set()
        assert self.release.wait(3)
        self.versions.append(snapshot.logical_version)
        self.rows[snapshot.intent_id] = snapshot.data()
        return True

    def get_workflow(self, intent_id):
        self.entered.set()
        assert self.release.wait(3)
        return self.rows.get(intent_id)


def snapshot(version, state="approved"):
    return WorkflowSnapshot.capture(
        intent_id="review",
        data={"state": state},
        logical_version=version,
        state=state,
        is_terminal=state == "expired",
        expires_at=None,
        retire_after=datetime.now(UTC) + timedelta(seconds=300),
    )


async def wait_entered(store):
    for _ in range(1000):
        if store.entered.is_set():
            return
        await asyncio.sleep(0.001)
    pytest.fail("worker did not start")


@pytest.mark.asyncio
async def test_outbox_coalesces_only_not_started_snapshots_and_has_hard_bound():
    store = Store()
    store.release.clear()
    runtime = WorkflowRuntime(store, capacity=1)
    runtime.enqueue(snapshot(1))
    await wait_entered(store)
    runtime.enqueue(snapshot(2))
    runtime.enqueue(snapshot(3, "expired"))
    with pytest.raises(ValueError, match="increase"):
        runtime.enqueue(snapshot(2))
    other = WorkflowSnapshot.capture(
        intent_id="other",
        data={},
        logical_version=1,
        state="denied",
        is_terminal=True,
        expires_at=None,
        retire_after=datetime.now(UTC) + timedelta(seconds=300),
    )
    with pytest.raises(RuntimeError, match="capacity"):
        runtime.enqueue(other)
    store.release.set()
    assert await runtime.flush("review", 3)
    assert store.versions == [1, 3]
    assert store.rows["review"]["state"] == "expired"
    await runtime.aclose()


@pytest.mark.asyncio
async def test_repeated_cancelled_status_read_and_close_drain_actual_worker():
    store = Store()
    store.release.clear()
    runtime = WorkflowRuntime(store, capacity=2)
    read = asyncio.create_task(runtime.read("get_workflow", "review"))
    await wait_entered(store)
    read.cancel()
    read.cancel()
    closing = asyncio.create_task(runtime.aclose())
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0.01)
    assert not read.done() and not closing.done()
    store.release.set()
    for task in (read, closing):
        with pytest.raises(asyncio.CancelledError):
            await task
    assert runtime._read_executor._shutdown and runtime._write_executor._shutdown


@pytest.mark.asyncio
async def test_request_cancellation_waits_for_final_cache_attempt_and_releases_active():
    service = _service()
    store = Store()
    store.release.clear()
    service._workflow_runtime.store = store
    pending = asyncio.create_task(service.propose_intent(_proposal("cache cancellation")))
    await wait_entered(store)
    pending.cancel()
    await asyncio.sleep(0.01)
    pending.cancel()
    await asyncio.sleep(0.01)
    assert not pending.done()
    assert service.council._active_reviews
    store.release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not service.council._active_reviews
    assert store.rows
    await service.aclose_workflows()


@pytest.mark.asyncio
async def test_finished_approved_reviews_do_not_consume_active_budget():
    service = _service()
    service.council.config.max_pending_intents = 1
    for index in range(5):
        result = await service.propose_intent(_proposal(str(index)))
        assert result.success, result.error
        assert not service.council._active_reviews
    assert len(service.council._workflows) == 5
    await service.aclose_workflows()


@pytest.mark.asyncio
async def test_unexpired_status_is_not_evicted_without_durable_receipt():
    service = _service()
    service.council.config.max_pending_intents = 1
    service.council.config.max_retained_workflows = 1
    clock = MockClock(datetime.now(UTC))
    service.council._clock = clock
    for index in range(2):
        assert (await service.propose_intent(_proposal(str(index)))).success
    blocked = await service.propose_intent(_proposal("full"))
    assert not blocked.success and "retention capacity" in blocked.error
    assert len(service.council._workflows) == 2
    clock.advance(seconds=601)
    # First sweep expires/enqueues; await attempts before eviction is safe.
    service.council.maintain_local_workflows()
    for key, workflow in list(service.council._workflows.items()):
        await service._workflow_runtime.flush(key, workflow.logical_version)
    service.council.maintain_local_workflows()
    assert not service.council._workflows
    assert not service.council._quorum_manager._records
    assert not service._workflow_runtime._events
    assert (await service.propose_intent(_proposal("available"))).success
    await service.aclose_workflows()


@pytest.mark.asyncio
async def test_loaded_status_never_overwrites_local_state_or_admission_owner():
    service = _service()
    store = Store()
    service._workflow_store = store
    service._workflow_runtime.store = store
    intent = Intent.create(
        agent_id="agent",
        action_type="system.noop",
        description="Status load race",
        target_resource="test",
        justification="status race",
    )
    result = service.council.submit_intent(intent)
    workflow = result.workflow
    await service._workflow_runtime.flush(intent.intent_id, workflow.logical_version)
    saved = service.council._workflows.pop(intent.intent_id)
    store.entered.clear()
    store.release.clear()
    pending = asyncio.create_task(
        service.check_intent_status_async(IntentStatusInput(intent_id=intent.intent_id))
    )
    await wait_entered(store)
    from nomos.level_4.council.workflow import WorkflowState

    saved.state = WorkflowState.EXPIRED
    service.council._workflows[intent.intent_id] = saved
    store.release.set()
    status = await pending
    assert status.state == "expired"
    await service.aclose_workflows()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["file", "postgres", "signer"])
async def test_legacy_io_guard_fails_before_any_request_mutation(tmp_path, backend):
    from nomos.level_1.ledger import FileLedgerStorage
    from nomos.level_1.ledger.postgres_storage import PostgresLedgerStorage

    service = _service()
    service.council.config.consolidated_review_audit = False
    if backend == "file":
        service.council.ledger._storage = FileLedgerStorage(tmp_path / "legacy.jsonl")
    elif backend == "postgres":
        service.council.ledger._storage = PostgresLedgerStorage(object(), "legacy")
    else:
        service.council.ledger._signer = object()
    with pytest.raises(RuntimeError, match="requires consolidated"):
        await service.propose_intent(_proposal("guard"))
    assert not service.council._submission_preflights
    assert not service.council._workflows
    assert not service._workflow_runtime._scheduled
    await service.aclose_workflows()


@pytest.mark.asyncio
async def test_normal_pending_response_keeps_open_quorum_until_expiry():
    from nomos.level_0.types import CouncilMember
    from nomos.level_4.council.workflow import WorkflowState

    service = _service()
    store = Store()
    service._workflow_runtime.store = store
    service.council.config.max_pending_intents = 1
    service.council.config.max_retained_workflows = 1
    service.council.register_council_member(
        CouncilMember(member_id="manual", public_key_hex="", display_name="Manual")
    )
    clock = MockClock(datetime.now(UTC))
    service.council._clock = clock

    async def pending_review(index):
        return service._submit_intent(
            Intent.create(
                agent_id="agent",
                action_type="system.noop",
                description=f"Pending review {index}",
                target_resource="test",
                justification="Preserve manual pending status",
            )
        )

    for index in range(2):
        result = await service._run_workflow_request(pending_review(index), "propose")
        assert result.workflow.state == WorkflowState.AWAITING_APPROVAL
    service.council.maintain_local_workflows()
    assert len(service.council._workflows) == 2
    assert len(service.council._quorum_manager._records) == 2
    assert not service.council._active_reviews
    assert not service.council.workflow_capacity_available()
    clock.advance(seconds=601)
    service.council.maintain_local_workflows()
    for key, workflow in list(service.council._workflows.items()):
        await service._workflow_runtime.flush(key, workflow.logical_version)
    service.council.maintain_local_workflows()
    assert not service.council._workflows
    assert not service.council._quorum_manager._records
    await service.aclose_workflows()


@pytest.mark.asyncio
async def test_status_read_waiters_are_bounded_and_queued_cancellation_releases_credit():
    store = Store()
    store.release.clear()
    runtime = WorkflowRuntime(store, capacity=2)
    first = asyncio.create_task(runtime.read("get_workflow", "one"))
    await wait_entered(store)
    second = asyncio.create_task(runtime.read("get_workflow", "two"))
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="capacity"):
        await runtime.read("get_workflow", "three")
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    assert runtime._read_waiters == 1
    store.release.set()
    assert await first is None
    assert runtime._read_waiters == 0
    await runtime.aclose()
