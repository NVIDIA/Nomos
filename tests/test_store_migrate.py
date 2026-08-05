# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the PostgreSQL migration runner."""

from __future__ import annotations

import pytest

from nomos.store.migrate import pending_migrations
from tests.postgres_test_support import (
    ensure_migrated_and_clean,
    postgres_dsn_for_tests,
    postgres_unavailable_reason,
)


class TestPendingMigrations:
    """Pure ordering/filtering logic — no database required."""

    def test_orders_lexicographically(self, tmp_path):
        (tmp_path / "0002_b.sql").write_text("SELECT 2;")
        (tmp_path / "0001_a.sql").write_text("SELECT 1;")
        (tmp_path / "0010_c.sql").write_text("SELECT 10;")
        names = [p.stem for p in pending_migrations(set(), tmp_path)]
        assert names == ["0001_a", "0002_b", "0010_c"]

    def test_filters_applied(self, tmp_path):
        (tmp_path / "0001_a.sql").write_text("SELECT 1;")
        (tmp_path / "0002_b.sql").write_text("SELECT 2;")
        names = [p.stem for p in pending_migrations({"0001_a"}, tmp_path)]
        assert names == ["0002_b"]

    def test_ignores_non_migration_files(self, tmp_path):
        (tmp_path / "0001_a.sql").write_text("SELECT 1;")
        (tmp_path / "README.md").write_text("not a migration")
        (tmp_path / "helper.sql").write_text("-- unnumbered, ignored")
        names = [p.stem for p in pending_migrations(set(), tmp_path)]
        assert names == ["0001_a"]

    def test_packaged_migrations_present_and_ordered(self):
        names = [p.stem for p in pending_migrations(set())]
        assert names[:2] == ["0001_durable_store", "0002_ledger"]
        assert names == sorted(names)


_PG_SKIP = postgres_unavailable_reason()


@pytest.mark.skipif(_PG_SKIP is not None, reason=_PG_SKIP or "")
class TestRunMigrations:
    """Integration tests against a real PostgreSQL."""

    def test_apply_is_idempotent(self):
        from nomos.store.migrate import run_migrations

        dsn = postgres_dsn_for_tests()
        ensure_migrated_and_clean(dsn)  # first application (or already applied)
        assert run_migrations(dsn) == []  # second run: nothing pending

    def test_schema_tables_exist(self):
        import psycopg

        dsn = postgres_dsn_for_tests()
        ensure_migrated_and_clean(dsn)
        with psycopg.connect(dsn) as conn:
            rows = conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            ).fetchall()
        tables = {r[0] for r in rows}
        assert {"schema_migrations", "accounts", "ledger_chains", "ledger_entries"} <= tables
