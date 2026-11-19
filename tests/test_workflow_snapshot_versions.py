# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

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
    # The insert is accepted only while the deadline is still ahead of the
    # database clock, and the sweep only once it is behind: leave room for a
    # slow first connection and for the two clocks to differ.
    item = snapshot(1, "denied", retirement=datetime.now(UTC) + timedelta(seconds=1.5))
    assert store.upsert_snapshot(item)
    time.sleep(1.7)
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


@pytest.mark.skipif(_PG_SKIP is not None, reason=_PG_SKIP or "")
def test_retirement_sweep_does_not_read_rows_that_are_not_due(pool, monkeypatch):
    """The sweep runs every second; its cost must follow the rows due, not the rows kept."""
    import psycopg

    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO council_workflows (council_id, intent_id, state, is_terminal, "
            "workflow_json, logical_version, retire_after) "
            "SELECT 'snapshot-test', 'kept-' || n, 'denied', true, '{}'::jsonb, 1, "
            "clock_timestamp() + make_interval(secs => 600 + n) FROM generate_series(1, 5000) AS n"
        )
        conn.execute("ANALYZE council_workflows")
    statements = []
    execute = psycopg.Connection.execute

    def recording(self, query, params=None, **kwargs):
        statements.append((query, params))
        return execute(self, query, params, **kwargs)

    monkeypatch.setattr(psycopg.Connection, "execute", recording)
    store = PostgresWorkflowStore(pool, council_id="snapshot-test")
    result = store.maintain_workflows(
        datetime.now(UTC), retention_seconds=300, limit=128, retire=True
    )
    monkeypatch.undo()
    assert result["retired"] == 0
    (sweep,) = [s for s in statements if "DELETE FROM council_workflows" in str(s[0])]

    def discarded(node):
        return node.get("Rows Removed by Filter", 0) + sum(
            discarded(child) for child in node.get("Plans", [])
        )

    with pool.connection() as conn:
        # Nothing is due, so running the statement again deletes nothing.
        plan = conn.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + sweep[0], sweep[1]).fetchone()[0]
    assert discarded(plan[0]["Plan"]) < 100, "the sweep read rows that were not due"


@pytest.mark.skipif(_PG_SKIP is not None, reason=_PG_SKIP or "")
def test_expiry_sweep_does_not_read_approvals_that_are_not_due(pool, monkeypatch):
    """Open approvals with a later deadline must not be read on every sweep."""
    import psycopg

    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO council_workflows (council_id, intent_id, state, is_terminal, "
            "workflow_json, logical_version, expires_at) "
            "SELECT 'snapshot-test', 'open-' || n, 'approved', false, '{}'::jsonb, 1, "
            "clock_timestamp() + make_interval(secs => 600 + n) FROM generate_series(1, 5000) AS n"
        )
        conn.execute("ANALYZE council_workflows")
    statements = []
    execute = psycopg.Connection.execute

    def recording(self, query, params=None, **kwargs):
        statements.append((query, params))
        return execute(self, query, params, **kwargs)

    monkeypatch.setattr(psycopg.Connection, "execute", recording)
    store = PostgresWorkflowStore(pool, council_id="snapshot-test")
    result = store.maintain_workflows(datetime.now(UTC), retention_seconds=300, limit=128)
    monkeypatch.undo()
    assert result["expired"] == 0
    (sweep,) = [s for s in statements if "SET state = 'expired'" in str(s[0])]

    def discarded(node):
        return node.get("Rows Removed by Filter", 0) + sum(
            discarded(child) for child in node.get("Plans", [])
        )

    with pool.connection() as conn:
        # Nothing is due, so running the statement again expires nothing.
        plan = conn.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + sweep[0], sweep[1]).fetchone()[0]
    assert discarded(plan[0]["Plan"]) < 100, "the sweep read approvals that were not due"


@pytest.mark.skipif(_PG_SKIP is not None, reason=_PG_SKIP or "")
def test_retirement_honours_each_kind_of_deadline_and_the_caller_cutoff(pool):
    store = PostgresWorkflowStore(pool, council_id="snapshot-test")
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO council_workflows (council_id, intent_id, state, is_terminal, "
            "workflow_json, logical_version, retire_after, updated_at) VALUES "
            "('snapshot-test', 'due', 'denied', true, '{}', 1, "
            "clock_timestamp() - interval '10 seconds', now()), "
            "('snapshot-test', 'promised', 'denied', true, '{}', 1, "
            "clock_timestamp() + interval '600 seconds', now()), "
            "('snapshot-test', 'legacy-old', 'denied', true, '{}', 0, NULL, "
            "clock_timestamp() - interval '400 seconds'), "
            "('snapshot-test', 'legacy-recent', 'denied', true, '{}', 0, NULL, "
            "clock_timestamp() - interval '100 seconds'), "
            "('snapshot-test', 'open', 'approved', false, '{}', 1, "
            "clock_timestamp() - interval '10 seconds', now())"
        )
    now = datetime.now(UTC)

    def retire(cutoff):
        return store.maintain_workflows(cutoff, retention_seconds=300, retire=True)["retired"]

    # A cutoff earlier than every deadline retires nothing.
    assert retire(now - timedelta(hours=1)) == 0
    # A cutoff in the future is clamped to database time: only what is due goes.
    assert retire(now + timedelta(days=1)) == 2
    with pool.connection() as conn:
        kept = conn.execute(
            "SELECT intent_id FROM council_workflows WHERE council_id = 'snapshot-test'"
        ).fetchall()
    assert {row[0] for row in kept} == {"promised", "legacy-recent", "open"}
