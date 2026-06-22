# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


"""Unit tests for PostgresWorkflowStore against a mocked connection pool.

The real-Postgres round-trips live in test_workflow_store_postgres.py (which skips
without a database). These exercise the store's SQL building, result mapping, and
the bounded-retry wrapper with a fake pool so the logic is covered even where no
test Postgres is configured.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

psycopg = pytest.importorskip("psycopg")

from nomos.store.workflow_store import (  # noqa: E402
    PostgresWorkflowStore,
    WorkflowRetryPolicy,
    WorkflowStoreError,
)


class _Cursor:
    def __init__(self, rows=None, rowcount=0):
        self._rows = rows or []
        self.rowcount = rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _Conn:
    """Fake psycopg connection: routes execute() through a scripted responder."""

    def __init__(self, responder):
        self._responder = responder
        self.executed: list[tuple] = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return self._responder(sql, params)

    @contextmanager
    def transaction(self):
        yield


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    @contextmanager
    def connection(self):
        yield self._conn


def _store(responder, **kw):
    conn = _Conn(responder)
    store = PostgresWorkflowStore(_Pool(conn), council_id="council-prod", **kw)
    return store, conn


def test_upsert_issues_insert_with_local_timeouts():
    store, conn = _store(lambda sql, params: _Cursor())
    store.upsert_workflow(
        "i1", {"state": "approved"}, state="approved", is_terminal=False, expires_at=None
    )
    sqls = " ".join(sql for sql, _ in conn.executed)
    assert "SET LOCAL lock_timeout" in sqls
    assert "INSERT INTO council_workflows" in sqls
    assert "ON CONFLICT (council_id, intent_id) DO UPDATE" in sqls


def test_get_workflow_maps_row_and_none():
    store, _ = _store(lambda sql, params: _Cursor(rows=[[{"a": 1}]]))
    assert store.get_workflow("i1") == {"a": 1}

    store_empty, _ = _store(lambda sql, params: _Cursor(rows=[]))
    assert store_empty.get_workflow("missing") is None


def test_list_by_state_maps_rows_and_short_circuits_empty():
    store, _conn = _store(lambda sql, params: _Cursor(rows=[[{"k": "a"}], [{"k": "b"}]]))
    assert store.list_workflows_by_state(["awaiting_approval"]) == [{"k": "a"}, {"k": "b"}]
    # empty state list must not touch the pool at all
    store2, conn2 = _store(lambda sql, params: _Cursor())
    assert store2.list_workflows_by_state([]) == []
    assert conn2.executed == []


def test_reap_returns_rowcount():
    store, _ = _store(lambda sql, params: _Cursor(rowcount=3))
    assert store.reap_terminal_before(datetime.now(UTC)) == 3


def test_retry_then_success():
    calls = {"n": 0}

    def responder(sql, params):
        if "SET LOCAL" in sql:
            return _Cursor()
        calls["n"] += 1
        if calls["n"] == 1:
            raise psycopg.OperationalError("transient")
        return _Cursor()

    store, _ = _store(
        responder, retry=WorkflowRetryPolicy(max_attempts=3, base_delay_s=0.0, total_deadline_s=5.0)
    )
    # Should swallow the first transient failure and succeed on retry.
    store.upsert_workflow("i1", {}, state="approved", is_terminal=False, expires_at=None)
    assert calls["n"] == 2


def test_retry_exhausted_raises_workflow_store_error():
    def responder(sql, params):
        if "SET LOCAL" in sql:
            return _Cursor()
        raise psycopg.OperationalError("always down")

    store, _ = _store(
        responder, retry=WorkflowRetryPolicy(max_attempts=2, base_delay_s=0.0, total_deadline_s=5.0)
    )
    with pytest.raises(WorkflowStoreError):
        store.get_workflow("i1")
