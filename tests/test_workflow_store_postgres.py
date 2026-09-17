# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""PostgreSQL integration tests for the shared workflow store.

Proves the replica-shared workflow status cache against a real database: a write
on one store instance (one replica) is visible to another; state filtering and the
expired-terminal reaper behave; ``council_id`` namespaces rows; and — end to end —
a SecurityCouncil that decides an intent persists it so a *fresh* council backed by
the same Postgres rehydrates it (the multi-replica check_intent_status path).

Skips cleanly when no test Postgres is configured (NOMOS_TEST_DATABASE_URL).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.postgres_test_support import (
    ensure_migrated_and_clean,
    postgres_dsn_for_tests,
    postgres_unavailable_reason,
)

_PG_SKIP = postgres_unavailable_reason()
pytestmark = pytest.mark.skipif(_PG_SKIP is not None, reason=_PG_SKIP or "")


@pytest.fixture
def pool():
    from psycopg_pool import ConnectionPool

    dsn = postgres_dsn_for_tests()
    ensure_migrated_and_clean(dsn)
    p = ConnectionPool(dsn, min_size=1, max_size=4, open=True)
    yield p
    p.close()


def _store(pool, council_id: str = "council-prod"):
    from nomos.store.workflow_store import PostgresWorkflowStore

    return PostgresWorkflowStore(pool, council_id=council_id)


def test_upsert_then_get_roundtrips(pool):
    store = _store(pool)
    store.upsert_workflow(
        "i1", {"a": 1, "state": "approved"}, state="approved", is_terminal=False, expires_at=None
    )
    assert store.get_workflow("i1") == {"a": 1, "state": "approved"}
    assert store.get_workflow("missing") is None


def test_write_visible_to_other_replica(pool):
    writer = _store(pool)
    reader = _store(pool)  # distinct instance, same DB == another replica
    writer.upsert_workflow(
        "i2", {"state": "completed", "n": 2}, state="completed", is_terminal=True, expires_at=None
    )
    assert reader.get_workflow("i2") == {"state": "completed", "n": 2}


def test_upsert_is_last_writer_wins_and_bumps_version(pool):
    store = _store(pool)
    store.upsert_workflow(
        "i3", {"v": 1}, state="awaiting_approval", is_terminal=False, expires_at=None
    )
    store.upsert_workflow("i3", {"v": 2}, state="approved", is_terminal=False, expires_at=None)
    assert store.get_workflow("i3") == {"v": 2}
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT state, version FROM council_workflows WHERE intent_id = 'i3'"
        ).fetchone()
    assert row == ("approved", 2)


def test_list_by_state_filters(pool):
    store = _store(pool)
    store.upsert_workflow(
        "a", {"k": "a"}, state="awaiting_approval", is_terminal=False, expires_at=None
    )
    store.upsert_workflow("b", {"k": "b"}, state="approved", is_terminal=False, expires_at=None)
    store.upsert_workflow(
        "c", {"k": "c"}, state="awaiting_approval", is_terminal=False, expires_at=None
    )
    got = store.list_workflows_by_state(["awaiting_approval"])
    assert sorted(d["k"] for d in got) == ["a", "c"]
    assert store.list_workflows_by_state([]) == []


def test_council_id_namespaces_rows(pool):
    a = _store(pool, "council-A")
    b = _store(pool, "council-B")
    a.upsert_workflow("dup", {"who": "A"}, state="approved", is_terminal=True, expires_at=None)
    b.upsert_workflow("dup", {"who": "B"}, state="approved", is_terminal=True, expires_at=None)
    assert a.get_workflow("dup") == {"who": "A"}
    assert b.get_workflow("dup") == {"who": "B"}


def test_reap_removes_only_expired_terminal(pool):
    store = _store(pool)
    now = datetime.now(UTC)
    store.upsert_workflow(
        "old", {}, state="completed", is_terminal=True, expires_at=now - timedelta(hours=1)
    )
    store.upsert_workflow(
        "future", {}, state="completed", is_terminal=True, expires_at=now + timedelta(hours=1)
    )
    store.upsert_workflow("noexp", {}, state="completed", is_terminal=True, expires_at=None)
    store.upsert_workflow(
        "pending", {}, state="awaiting_approval", is_terminal=False, expires_at=now
    )

    assert store.reap_terminal_before(now) == 1
    assert store.get_workflow("old") is None
    assert store.get_workflow("future") is not None
    assert store.get_workflow("noexp") is not None
    assert store.get_workflow("pending") is not None


def test_council_writethrough_rehydrate_over_postgres(pool):
    """End to end: decide on one council, rehydrate on a fresh one via Postgres."""
    from nomos.level_0.types import AgentRole, CouncilMember, Intent
    from nomos.level_3.executor import NoOpHandler
    from nomos.level_4.council import CouncilConfig, SecurityCouncil, WorkflowState

    def _make_replica() -> SecurityCouncil:
        council = SecurityCouncil(
            config=CouncilConfig(council_id="council-prod", require_justification=True),
            workflow_store=_store(pool, "council-prod"),
        )
        council.register_action_handler(NoOpHandler())
        council.register_agent("agent-1", AgentRole.APPLICATION)
        for mid, key in (("alice", "abc"), ("bob", "def")):
            council.register_council_member(
                CouncilMember(member_id=mid, public_key_hex=key, display_name=mid, weight=1)
            )
        return council

    intent = Intent.create(
        agent_id="agent-1",
        action_type="system.noop",
        description="postgres write-through rehydrate test",
        target_resource="/tmp/test",
        justification="nomos-5b pg integration",
    )

    decider = _make_replica()
    assert decider.submit_intent(intent).requires_approval
    decider.approve_intent(intent.intent_id, "alice")
    decider.approve_intent(intent.intent_id, "bob")
    assert decider.execute_intent(intent.intent_id).success

    # A different replica with an empty local cache resolves the status from PG.
    other = _make_replica()
    assert intent.intent_id not in other._workflows
    rehydrated = other.get_workflow(intent.intent_id)
    assert rehydrated is not None
    assert rehydrated.state == WorkflowState.COMPLETED
    assert rehydrated.intent_id == intent.intent_id
