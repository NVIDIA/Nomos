"""Immutable workflow snapshots and terminal-preserving durable ordering."""

import time
from datetime import UTC, datetime, timedelta

import pytest

from nomos.level_0.types.workflow_snapshot import WorkflowSnapshot
from nomos.store.workflow_store import PostgresWorkflowStore
from tests.postgres_test_support import postgres_unavailable_reason
from tests.test_postgres_ledger import pool as pool

_PG_SKIP = postgres_unavailable_reason()


def snapshot(version, state="approved", *, retirement=None, expiry=None):
    now = datetime.now(UTC)
    return WorkflowSnapshot.capture(
        intent_id="intent-snapshot",
        data={"intent": {"intent_id": "intent-snapshot"}, "state": state, "metadata": {}},
        logical_version=version,
        state=state,
        is_terminal=state in {"expired", "denied", "failed"},
        expires_at=expiry,
        retire_after=retirement or now + timedelta(seconds=60),
    )


def test_snapshot_detaches_nested_mutable_data():
    data = {"state": "approved", "metadata": {"ids": ["old"]}}
    item = WorkflowSnapshot.capture(
        intent_id="intent-snapshot",
        data=data,
        logical_version=1,
        state="approved",
        is_terminal=False,
        expires_at=None,
        retire_after=datetime.now(UTC) + timedelta(seconds=60),
    )
    data["metadata"]["ids"].append("new")
    decoded = item.data()
    decoded["metadata"]["ids"].append("another")
    assert item.data()["metadata"]["ids"] == ["old"]
    assert item.data()["logical_version"] == 1


@pytest.mark.skipif(_PG_SKIP is not None, reason=_PG_SKIP or "")
def test_versions_and_terminal_state_both_guard_writes(pool):
    store = PostgresWorkflowStore(pool, council_id="snapshot-test")
    assert store.upsert_snapshot(snapshot(2))
    assert not store.upsert_snapshot(snapshot(1, "denied"))
    assert store.upsert_snapshot(snapshot(3, "expired"))
    assert not store.upsert_snapshot(snapshot(999, "approved"))
    assert not store.upsert_snapshot(snapshot(1000, "denied"))
    assert store.get_workflow("intent-snapshot")["state"] == "expired"


@pytest.mark.skipif(_PG_SKIP is not None, reason=_PG_SKIP or "")
def test_retired_snapshot_cannot_reinsert_deleted_row(pool):
    store = PostgresWorkflowStore(pool, council_id="snapshot-test")
    item = snapshot(1, "denied", retirement=datetime.now(UTC) + timedelta(seconds=0.1))
    assert store.upsert_snapshot(item)
    time.sleep(0.12)
    assert (
        store.maintain_workflows(datetime.now(UTC), retention_seconds=300, limit=1)["retired"] == 0
    )
    assert store.get_workflow(item.intent_id) is not None
    with pool.connection() as conn:
        assert (
            conn.execute(
                "DELETE FROM council_workflows WHERE council_id = %s", ("snapshot-test",)
            ).rowcount
            == 0
        )
    result = store.maintain_workflows(
        datetime.now(UTC), retention_seconds=300, limit=1, retire=True
    )
    assert result["retired"] == 1
    assert not store.upsert_snapshot(item)
    assert store.get_workflow(item.intent_id) is None


@pytest.mark.skipif(_PG_SKIP is not None, reason=_PG_SKIP or "")
def test_expiry_is_bounded_and_stale_approval_cannot_resurrect(pool):
    store = PostgresWorkflowStore(pool, council_id="snapshot-test")
    assert store.upsert_snapshot(snapshot(1, expiry=datetime.now(UTC) - timedelta(seconds=1)))
    result = store.maintain_workflows(datetime.now(UTC), retention_seconds=300, limit=1)
    assert result["expired"] == 1
    row = store.get_workflow("intent-snapshot")
    assert row["state"] == "expired" and row["logical_version"] == 2
    assert not store.upsert_snapshot(snapshot(3))


@pytest.mark.skipif(_PG_SKIP is not None, reason=_PG_SKIP or "")
def test_retirement_horizon_cannot_shrink_and_old_writer_cannot_undo_expiry(pool):
    store = PostgresWorkflowStore(pool, council_id="snapshot-test")
    later = datetime.now(UTC) + timedelta(seconds=600)
    initial = snapshot(1, retirement=later)
    assert store.upsert_snapshot(initial)
    assert not store.upsert_snapshot(snapshot(2, retirement=later - timedelta(seconds=270)))
    final = snapshot(2, "expired", retirement=later)
    assert store.upsert_snapshot(final)
    # The old rolling-version API has no logical generation at all.
    store.upsert_workflow(
        initial.intent_id, initial.data(), state="approved", is_terminal=False, expires_at=None
    )
    assert store.get_workflow(initial.intent_id)["state"] == "expired"
    with pool.connection() as conn:
        assert (
            conn.execute(
                "DELETE FROM council_workflows WHERE council_id = %s", ("snapshot-test",)
            ).rowcount
            == 0
        )
    assert store.get_workflow(initial.intent_id)["state"] == "expired"


@pytest.mark.skipif(_PG_SKIP is not None, reason=_PG_SKIP or "")
def test_same_version_conflict_does_not_succeed(pool):
    store = PostgresWorkflowStore(pool, council_id="snapshot-test")
    item = snapshot(1)
    assert store.upsert_snapshot(item)
    assert not store.upsert_snapshot(item)
    assert not store.upsert_snapshot(snapshot(1, "denied"))
    assert store.get_workflow(item.intent_id) == item.data()
