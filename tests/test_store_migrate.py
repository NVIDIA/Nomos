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


class TestConcurrentIndexMigrations:
    """Recognising a no-transaction migration — no database required."""

    def test_ordinary_migration_is_not_a_concurrent_build(self):
        from nomos.store.migrate import concurrent_index_name

        assert concurrent_index_name("CREATE INDEX CONCURRENTLY i ON t (v);") is None
        assert concurrent_index_name("ALTER TABLE t ADD COLUMN v int;") is None

    def test_marked_migration_names_its_index(self):
        from nomos.store.migrate import NO_TRANSACTION_MARKER, concurrent_index_name

        text = (
            "-- licence line\n"
            f"{NO_TRANSACTION_MARKER}\n\n"
            "-- why the index exists; a semicolon here is fine;\n"
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS t_v_idx\n"
            "    ON t (v) WHERE v > 0;\n"
        )
        assert concurrent_index_name(text) == "t_v_idx"

    def test_index_name_is_the_one_postgresql_stores(self):
        from nomos.store.migrate import NO_TRANSACTION_MARKER, concurrent_index_name

        # An unquoted name is folded to lower case before it reaches the catalog.
        build = "CREATE INDEX CONCURRENTLY IF NOT EXISTS T_V_Idx ON t (v);"
        assert concurrent_index_name(f"{NO_TRANSACTION_MARKER}\n{build}\n") == "t_v_idx"

    @pytest.mark.parametrize("target", ["t", "T", "ONLY t"])
    def test_marked_migration_names_the_table_whose_index_it_builds(self, target):
        from nomos.store.migrate import NO_TRANSACTION_MARKER, concurrent_index_table

        build = f"CREATE INDEX CONCURRENTLY IF NOT EXISTS t_v_idx ON {target} (v);"
        assert concurrent_index_table(f"{NO_TRANSACTION_MARKER}\n{build}\n") == "t"
        assert concurrent_index_table(build) is None

    @pytest.mark.parametrize(
        "statement",
        [
            "CREATE INDEX IF NOT EXISTS i ON t (v);",  # would lock the table
            "CREATE INDEX CONCURRENTLY i ON t (v);",  # not repeatable
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS i ON t (v); DROP TABLE t;",
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS \u00c9 ON t (v);",  # not ASCII
            "ALTER TABLE t ADD COLUMN v int;",
            "",
        ],
    )
    def test_marked_migration_must_be_one_repeatable_concurrent_build(self, statement):
        from nomos.store.migrate import NO_TRANSACTION_MARKER, concurrent_index_name

        with pytest.raises(ValueError, match="exactly one"):
            concurrent_index_name(f"{NO_TRANSACTION_MARKER}\n{statement}\n")

    @pytest.mark.parametrize("target", ["audit.t", "audit . t", '"audit".t'])
    def test_marked_migration_must_not_name_the_schema_of_its_table(self, target):
        """An index lives in its table's schema; a failed build is looked for in the current one."""
        from nomos.store.migrate import NO_TRANSACTION_MARKER, concurrent_index_name

        build = f"CREATE INDEX CONCURRENTLY IF NOT EXISTS t_v_idx ON {target} (v);"
        with pytest.raises(ValueError, match="without a schema"):
            concurrent_index_name(f"{NO_TRANSACTION_MARKER}\n{build}\n")

    def test_marked_migration_must_not_name_an_index_postgresql_would_truncate(self):
        """PostgreSQL stores 63 bytes of a name; a failed build is looked for by the name given."""
        from nomos.store.migrate import NO_TRANSACTION_MARKER, concurrent_index_name

        fits, too_long = "i" * 63, "i" * 64
        build = "CREATE INDEX CONCURRENTLY IF NOT EXISTS {} ON t (v);"
        assert concurrent_index_name(f"{NO_TRANSACTION_MARKER}\n{build.format(fits)}\n") == fits
        with pytest.raises(ValueError, match="63"):
            concurrent_index_name(f"{NO_TRANSACTION_MARKER}\n{build.format(too_long)}\n")

    @pytest.mark.parametrize("target", ["t (v)", "t(v)", "ONLY t (v)", "t USING btree (v)"])
    def test_marked_migration_may_index_a_table_of_the_current_schema(self, target):
        from nomos.store.migrate import NO_TRANSACTION_MARKER, concurrent_index_name

        build = f"CREATE INDEX CONCURRENTLY IF NOT EXISTS t_v_idx ON {target};"
        assert concurrent_index_name(f"{NO_TRANSACTION_MARKER}\n{build}\n") == "t_v_idx"

    def test_packaged_workflow_indexes_are_built_outside_the_table_lock(self):
        from nomos.store.migrate import concurrent_index_name

        by_name = {p.stem: p.read_text() for p in pending_migrations(set())}
        # The schema change holds an exclusive lock, so it must not build indexes.
        assert "CREATE INDEX" not in by_name["0005_workflow_retention"]
        assert (
            concurrent_index_name(by_name["0007_workflow_retirement_index"])
            == "council_workflows_retirement_idx"
        )
        assert (
            concurrent_index_name(by_name["0008_workflow_decision_expiry_index"])
            == "council_workflows_decision_expiry_idx"
        )


@pytest.fixture
def scratch_schema():
    """An empty schema, and a DSN whose connections resolve names in it."""
    import uuid

    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    name = f"migrate_test_{uuid.uuid4().hex[:12]}"
    dsn = postgres_dsn_for_tests()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    try:
        yield make_conninfo(dsn, options=f"-c search_path={name}")
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


def _index_validity(dsn: str) -> dict[str, bool]:
    import psycopg

    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            """
            SELECT c.relname, i.indisvalid FROM pg_index i
              JOIN pg_class c ON c.oid = i.indexrelid
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = current_schema()
            """
        ).fetchall()
    return dict(rows)


def _applied(dsn: str) -> set[str]:
    import psycopg

    with psycopg.connect(dsn) as conn:
        return {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}


@pytest.mark.skipif(_PG_SKIP is not None, reason=_PG_SKIP or "")
class TestConcurrentIndexBuilds:
    """The no-transaction path against a real PostgreSQL, in a scratch schema."""

    def test_fresh_database_gets_both_workflow_indexes_valid(self, scratch_schema):
        from nomos.store.migrate import run_migrations

        applied = run_migrations(scratch_schema)

        assert applied == sorted(applied) and applied[0] == "0001_durable_store"
        indexes = _index_validity(scratch_schema)
        assert indexes["council_workflows_retirement_idx"] is True
        assert indexes["council_workflows_decision_expiry_idx"] is True
        assert run_migrations(scratch_schema) == []

    def test_database_that_already_has_the_indexes_records_them_without_rebuilding(
        self, scratch_schema
    ):
        """A database migrated when 0005 still created the indexes itself."""
        import psycopg

        from nomos.store.migrate import run_migrations

        run_migrations(scratch_schema)
        split_out = ["0007_workflow_retirement_index", "0008_workflow_decision_expiry_index"]
        with psycopg.connect(scratch_schema) as conn:
            before = conn.execute(
                "SELECT oid FROM pg_class WHERE relname = 'council_workflows_retirement_idx'"
                " AND relnamespace = (SELECT oid FROM pg_namespace"
                " WHERE nspname = current_schema())"
            ).fetchone()
            conn.execute("DELETE FROM schema_migrations WHERE version = ANY(%s)", (split_out,))

        assert run_migrations(scratch_schema) == split_out

        with psycopg.connect(scratch_schema) as conn:
            after = conn.execute(
                "SELECT oid FROM pg_class WHERE relname = 'council_workflows_retirement_idx'"
                " AND relnamespace = (SELECT oid FROM pg_namespace"
                " WHERE nspname = current_schema())"
            ).fetchone()
        assert after == before

    @pytest.mark.parametrize("written", ["t_v_idx", "T_V_IDX"])
    def test_interrupted_build_is_rebuilt_not_mistaken_for_finished(
        self, scratch_schema, tmp_path, written
    ):
        import psycopg

        from nomos.store.migrate import NO_TRANSACTION_MARKER, run_migrations

        (tmp_path / "0001_table.sql").write_text(
            "CREATE TABLE t (v int); INSERT INTO t VALUES (1), (1), (2);"
        )
        assert run_migrations(scratch_schema, migrations_dir=tmp_path) == ["0001_table"]
        # PostgreSQL stores either spelling as t_v_idx.
        build = f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {written} ON t (v);"
        (tmp_path / "0002_index.sql").write_text(f"{NO_TRANSACTION_MARKER}\n{build}\n")

        # Duplicate values fail the build and leave an INVALID index behind,
        # which is what an interrupted concurrent build leaves too.
        with pytest.raises(psycopg.errors.UniqueViolation):
            run_migrations(scratch_schema, migrations_dir=tmp_path)
        assert _index_validity(scratch_schema)["t_v_idx"] is False
        assert _applied(scratch_schema) == {"0001_table"}

        with psycopg.connect(scratch_schema) as conn:
            conn.execute("DELETE FROM t WHERE ctid IN (SELECT min(ctid) FROM t WHERE v = 1)")

        assert run_migrations(scratch_schema, migrations_dir=tmp_path) == ["0002_index"]
        assert _index_validity(scratch_schema)["t_v_idx"] is True
        assert _applied(scratch_schema) == {"0001_table", "0002_index"}

    def test_interrupted_build_is_found_in_the_schema_its_table_resolves_to(
        self, scratch_schema, tmp_path
    ):
        """An unqualified table can resolve to a later schema of the search path.

        The index is created beside its table, not in the current schema, and an
        interrupted build has to be found there.
        """
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo

        from nomos.store.migrate import NO_TRANSACTION_MARKER, run_migrations

        with psycopg.connect(scratch_schema) as conn:
            first = conn.execute("SELECT current_schema()").fetchone()[0]
        second = f"{first}_far"
        dsn = make_conninfo(scratch_schema, options=f"-c search_path={first},{second}")
        build = "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS t_v_idx ON t (v);"
        (tmp_path / "0001_index.sql").write_text(f"{NO_TRANSACTION_MARKER}\n{build}\n")

        def far_index_is_valid() -> bool:
            with psycopg.connect(dsn) as conn:
                row = conn.execute(
                    "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid"
                    " JOIN pg_namespace n ON n.oid = c.relnamespace"
                    " WHERE c.relname = 't_v_idx' AND n.nspname = %s",
                    (second,),
                ).fetchone()
            return bool(row and row[0])

        with psycopg.connect(scratch_schema, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(second)))
        try:
            with psycopg.connect(dsn) as conn:
                table = sql.Identifier(second, "t")
                conn.execute(sql.SQL("CREATE TABLE {} (v int)").format(table))
                conn.execute(sql.SQL("INSERT INTO {} VALUES (1), (1), (2)").format(table))

            with pytest.raises(psycopg.errors.UniqueViolation):
                run_migrations(dsn, migrations_dir=tmp_path)
            assert not far_index_is_valid()

            with psycopg.connect(dsn) as conn:
                conn.execute("DELETE FROM t WHERE ctid IN (SELECT min(ctid) FROM t WHERE v = 1)")

            assert run_migrations(dsn, migrations_dir=tmp_path) == ["0001_index"]
            assert far_index_is_valid()
        finally:
            with psycopg.connect(scratch_schema, autocommit=True) as conn:
                conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(second)))

    @pytest.mark.parametrize("earlier_is_valid", [True, False])
    def test_interrupted_build_is_not_confused_with_an_index_of_an_earlier_schema(
        self, scratch_schema, tmp_path, earlier_is_valid
    ):
        """A same-named index of another table can sit earlier on the search path.

        PostgreSQL checks IF NOT EXISTS in the schema of the table being indexed.
        The cleanup has to look there as well: a valid index found first by name
        would hide the interrupted build, and an invalid one would be dropped in
        its place.
        """
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo

        from nomos.store.migrate import NO_TRANSACTION_MARKER, run_migrations

        with psycopg.connect(scratch_schema) as conn:
            first = conn.execute("SELECT current_schema()").fetchone()[0]
        second = f"{first}_far"
        dsn = make_conninfo(scratch_schema, options=f"-c search_path={first},{second}")
        build = "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS t_v_idx ON t (v);"
        (tmp_path / "0001_index.sql").write_text(f"{NO_TRANSACTION_MARKER}\n{build}\n")

        def index_in(schema: str) -> tuple[int, bool] | None:
            with psycopg.connect(dsn) as conn:
                return conn.execute(
                    "SELECT c.oid, i.indisvalid FROM pg_index i"
                    " JOIN pg_class c ON c.oid = i.indexrelid"
                    " JOIN pg_namespace n ON n.oid = c.relnamespace"
                    " WHERE c.relname = 't_v_idx' AND n.nspname = %s",
                    (schema,),
                ).fetchone()

        with psycopg.connect(scratch_schema, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(second)))
            # Another table of the first schema already has an index of that name.
            conn.execute("CREATE TABLE other (v int)")
            if earlier_is_valid:
                conn.execute("CREATE UNIQUE INDEX t_v_idx ON other (v)")
            else:
                conn.execute("INSERT INTO other VALUES (1), (1)")
                with pytest.raises(psycopg.errors.UniqueViolation):
                    conn.execute("CREATE UNIQUE INDEX CONCURRENTLY t_v_idx ON other (v)")
        try:
            earlier = index_in(first)
            assert earlier is not None and earlier[1] is earlier_is_valid
            with psycopg.connect(dsn) as conn:
                table = sql.Identifier(second, "t")
                conn.execute(sql.SQL("CREATE TABLE {} (v int)").format(table))
                conn.execute(sql.SQL("INSERT INTO {} VALUES (1), (1), (2)").format(table))

            with pytest.raises(psycopg.errors.UniqueViolation):
                run_migrations(dsn, migrations_dir=tmp_path)
            interrupted = index_in(second)
            assert interrupted is not None and interrupted[1] is False

            with psycopg.connect(dsn) as conn:
                conn.execute("DELETE FROM t WHERE ctid IN (SELECT min(ctid) FROM t WHERE v = 1)")

            assert run_migrations(dsn, migrations_dir=tmp_path) == ["0001_index"]
            rebuilt = index_in(second)
            assert rebuilt is not None and rebuilt[1] is True
            # The other table's index is the same one, in the state it was in.
            assert index_in(first) == earlier
        finally:
            with psycopg.connect(scratch_schema, autocommit=True) as conn:
                conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(second)))

    def test_malformed_no_transaction_migration_is_refused_before_it_runs(
        self, scratch_schema, tmp_path
    ):
        from nomos.store.migrate import NO_TRANSACTION_MARKER, run_migrations

        (tmp_path / "0001_table.sql").write_text("CREATE TABLE t (v int);")
        (tmp_path / "0002_bad.sql").write_text(f"{NO_TRANSACTION_MARKER}\nDROP TABLE t;\n")

        with pytest.raises(ValueError, match=r"0002_bad\.sql: .*exactly one"):
            run_migrations(scratch_schema, migrations_dir=tmp_path)
        assert _applied(scratch_schema) == {"0001_table"}

    def test_later_migrations_still_apply_after_a_concurrent_build(self, scratch_schema, tmp_path):
        from nomos.store.migrate import NO_TRANSACTION_MARKER, run_migrations

        (tmp_path / "0001_table.sql").write_text("CREATE TABLE t (v int);")
        (tmp_path / "0002_index.sql").write_text(
            f"{NO_TRANSACTION_MARKER}\nCREATE INDEX CONCURRENTLY IF NOT EXISTS t_v_idx ON t (v);\n"
        )
        (tmp_path / "0003_column.sql").write_text("ALTER TABLE t ADD COLUMN w int;")

        assert run_migrations(scratch_schema, migrations_dir=tmp_path) == [
            "0001_table",
            "0002_index",
            "0003_column",
        ]
        assert _index_validity(scratch_schema)["t_v_idx"] is True

    def test_replicas_starting_together_all_finish_and_apply_each_migration_once(
        self, scratch_schema
    ):
        """A waiting runner must not hold a snapshot the concurrent build waits for.

        With a blocking advisory lock the waiters and the index build waited on
        each other and PostgreSQL ended the waiters as deadlocks.
        """
        from concurrent.futures import ThreadPoolExecutor

        from nomos.store.migrate import run_migrations

        with ThreadPoolExecutor(max_workers=3) as pool:
            results = [
                future.result(timeout=60)
                for future in [pool.submit(run_migrations, scratch_schema) for _ in range(3)]
            ]

        applied = sorted(version for result in results for version in result)
        assert applied == sorted(p.stem for p in pending_migrations(set()))
        indexes = _index_validity(scratch_schema)
        assert indexes["council_workflows_retirement_idx"] is True
        assert indexes["council_workflows_decision_expiry_idx"] is True
