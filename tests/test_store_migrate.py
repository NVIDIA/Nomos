# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the PostgreSQL migration runner."""

from __future__ import annotations

from pathlib import Path

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

    def test_context_redaction_migration_enforces_rolling_upgrade_invariant(self):
        migration = (
            Path(__file__).parents[1] / "nomos/store/migrations/0005_redact_workflow_context.sql"
        ).read_text()

        assert "UPDATE council_workflows" in migration
        assert "council_workflows_intent_context_redacted" in migration
        assert "workflow_json #>> '{intent,context}'" in migration
        assert "ledger_entries_intent_context_redacted" in migration
        assert "payload_json #>> '{intent,context}'" in migration
        assert ") NOT VALID;" in migration


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

    def test_context_constraints_reject_new_raw_workflow_and_ledger_rows(self):
        import json

        import psycopg

        dsn = postgres_dsn_for_tests()
        ensure_migrated_and_clean(dsn)

        with psycopg.connect(dsn) as conn:
            with pytest.raises(psycopg.errors.CheckViolation):
                with conn.transaction():
                    conn.execute(
                        "INSERT INTO council_workflows "
                        "(council_id, intent_id, state, workflow_json) "
                        "VALUES (%s, %s, %s, %s::jsonb)",
                        (
                            "privacy-test",
                            "intent-raw-workflow",
                            "approved",
                            json.dumps({"intent": {"context": "PRIVATE"}}),
                        ),
                    )

            conn.execute(
                "INSERT INTO ledger_chains (chain_id, council_id, created_by) VALUES (%s, %s, %s)",
                ("privacy-chain", "privacy-test", "pytest"),
            )
            with pytest.raises(psycopg.errors.CheckViolation):
                with conn.transaction():
                    conn.execute(
                        "INSERT INTO ledger_entries "
                        "(chain_id, sequence, entry_type, payload_json, entry_hash, "
                        "writer_instance_id, entry_timestamp_iso) "
                        "VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)",
                        (
                            "privacy-chain",
                            0,
                            "intent_submitted",
                            json.dumps({"context": "PRIVATE"}),
                            "privacy-hash",
                            "pytest",
                            "2026-09-30T00:00:00+00:00",
                        ),
                    )
