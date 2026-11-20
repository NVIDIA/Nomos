# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The store keeps answering after PostgreSQL drops its pooled connections.

A database restart or failover closes every pooled connection from the far
end. Each one used to fail the first request that was handed it.
"""

import asyncio
import os

import psycopg
import pytest
import pytest_asyncio

from nomos.store.postgres_store import PostgresStore
from tests.postgres_test_support import ensure_migrated_and_clean


@pytest_asyncio.fixture
async def store():
    dsn = os.environ.get("NOMOS_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("NOMOS_TEST_DATABASE_URL is not set")
    ensure_migrated_and_clean(dsn)
    store = PostgresStore(dsn, account_integrity_key="reconnect-test-key", pool_min_size=4)
    await store.connect()
    try:
        yield store, dsn
    finally:
        await store.close()


def _drop_every_other_session(dsn: str) -> int:
    with psycopg.connect(dsn, autocommit=True) as admin:
        rows = admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid()"
        ).fetchall()
    return len(rows)


@pytest.mark.asyncio
async def test_reads_succeed_after_every_pooled_connection_was_dropped(store):
    store, dsn = store
    # Use the pool so its connections are open and idle.
    await asyncio.gather(*(store.get_account(f"warm-{i}") for i in range(4)))
    assert _drop_every_other_session(dsn) >= 4

    results = await asyncio.gather(
        *(store.get_account(f"missing-{i}") for i in range(8)), return_exceptions=True
    )

    assert [r for r in results if isinstance(r, BaseException)] == []
