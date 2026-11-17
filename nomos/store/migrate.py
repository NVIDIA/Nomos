# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
PostgreSQL schema migration runner.

Deliberately minimal — no ORM, no alembic. Migrations are ordered SQL files
(``NNNN_name.sql``) shipped as package data under ``nomos/store/migrations/``.
Applied versions are tracked in a ``schema_migrations`` table; a session-level
advisory lock serializes replicas that race to migrate at startup.

Each file runs in one transaction with its ``schema_migrations`` row. The one
exception is an index built on a table that is already in use: a file whose
header carries ``-- migrate: no-transaction`` holds a single
``CREATE INDEX CONCURRENTLY IF NOT EXISTS`` statement and runs outside a
transaction, so the build blocks neither reads nor writes of that table.

Usage:
    python -m nomos.store.migrate --dsn postgresql://...   # or COUNCIL_DATABASE_URL
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MIGRATIONS_DIR = Path(__file__).parent / "migrations"
# Arbitrary stable 64-bit key ("NOMO") for the migration advisory lock.
MIGRATION_LOCK_KEY = 0x4E4F4D4F
MIGRATION_LOCK_POLL_SECONDS = 0.25
NO_TRANSACTION_MARKER = "-- migrate: no-transaction"
# The table is named without a schema. PostgreSQL puts an index in its table's
# schema, and an interrupted build is looked for among the indexes of the table
# that name resolves to. The index name must be one PostgreSQL stores whole:
# 63 bytes.
_CONCURRENT_INDEX = re.compile(
    r"\ACREATE\s+(?:UNIQUE\s+)?INDEX\s+CONCURRENTLY\s+IF\s+NOT\s+EXISTS\s+(\w{1,63})"
    r"\s+ON\s+(?:ONLY\s+)?(\w+)\s*(?:\(|USING\s)[^;]*;?\Z",
    re.IGNORECASE | re.ASCII,
)


def pending_migrations(
    applied: set[str], migrations_dir: Path = DEFAULT_MIGRATIONS_DIR
) -> list[Path]:
    """Return unapplied migration files in lexicographic (version) order."""
    files = sorted(p for p in migrations_dir.glob("[0-9]*.sql") if p.is_file())
    return [p for p in files if p.stem not in applied]


def _concurrent_index(migration_sql: str) -> tuple[str, str] | None:
    """Index and table of a no-transaction migration; None for an ordinary one.

    Raises ValueError when the file is marked no-transaction but is anything
    other than one ``CREATE INDEX CONCURRENTLY IF NOT EXISTS`` statement on a
    table named without a schema, or names an index longer than 63 characters.
    """
    lines = migration_sql.splitlines()
    if not any(line.strip() == NO_TRANSACTION_MARKER for line in lines):
        return None
    statement = "\n".join(line for line in lines if not line.lstrip().startswith("--")).strip()
    match = _CONCURRENT_INDEX.match(statement)
    if match is None:
        raise ValueError(
            "a no-transaction migration must be exactly one "
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS statement, "
            "with an index name of at most 63 characters, "
            "on a table named without a schema"
        )
    # PostgreSQL folds an unquoted ASCII name to lower case, and the lookup for
    # an interrupted build has to use the names as the catalog holds them.
    return match.group(1).lower(), match.group(2).lower()


def concurrent_index_name(migration_sql: str) -> str | None:
    """Index built by a no-transaction migration; None for an ordinary migration.

    Raises ValueError for a malformed no-transaction migration.
    """
    built = _concurrent_index(migration_sql)
    return None if built is None else built[0]


def concurrent_index_table(migration_sql: str) -> str | None:
    """Table a no-transaction migration indexes; None for an ordinary migration.

    Raises ValueError for a malformed no-transaction migration.
    """
    built = _concurrent_index(migration_sql)
    return None if built is None else built[1]


def _build_index_concurrently(
    conn: Any, version: str, migration_sql: str, index: str, table: str
) -> None:
    """Run one concurrent index build and record it.

    PostgreSQL refuses CONCURRENTLY inside a transaction, so the build and its
    schema_migrations row cannot commit together; the build is repeatable
    instead. An interrupted build leaves an INVALID index under the same name,
    which IF NOT EXISTS would accept as finished, so it is dropped first.
    """
    from psycopg import sql

    conn.commit()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            # The index sits beside its table, and an unqualified table may
            # resolve to any schema of the search path. The table is resolved
            # first and the index is looked for among its own: an index of the
            # same name on another table, in a schema earlier on the path, is
            # neither taken for this build nor dropped. The drop names the
            # schema the index was found in. A table that does not resolve
            # matches nothing, and the build then fails on its own.
            cur.execute(
                """
                SELECT n.nspname FROM pg_index i
                  JOIN pg_class c ON c.oid = i.indexrelid
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE i.indrelid = to_regclass(quote_ident(%s))
                   AND c.relname = %s AND NOT i.indisvalid
                """,
                (table, index),
            )
            invalid = cur.fetchone()
            if invalid is not None:
                logger.warning("dropping invalid index %s left by an interrupted build", index)
                cur.execute(
                    sql.SQL("DROP INDEX CONCURRENTLY IF EXISTS {}").format(
                        sql.Identifier(invalid[0], index)
                    )
                )
            cur.execute(migration_sql)
            cur.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
    finally:
        conn.autocommit = False


def _acquire_migration_lock(conn: Any) -> None:
    """Wait for the migration lock without holding a snapshot while waiting.

    A session blocked inside ``pg_advisory_lock`` keeps the snapshot of that
    statement, and CREATE INDEX CONCURRENTLY in the lock holder waits for every
    older snapshot in the database: the two would wait on each other until
    PostgreSQL killed the waiter as a deadlock. Polling the non-blocking form,
    one statement per attempt in autocommit, leaves nothing open between tries.
    """
    conn.autocommit = True
    try:
        waiting = False
        while True:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
                row = cur.fetchone()
            if row is not None and row[0]:
                return
            if not waiting:
                logger.info("another process is applying migrations; waiting")
                waiting = True
            time.sleep(MIGRATION_LOCK_POLL_SECONDS)
    finally:
        conn.autocommit = False


def run_migrations(
    dsn: str,
    *,
    migrations_dir: Path = DEFAULT_MIGRATIONS_DIR,
    dry_run: bool = False,
) -> list[str]:
    """Apply unapplied migrations in order; return the versions applied.

    Safe to call from multiple replicas concurrently: a session advisory lock
    makes losers wait (see ``_acquire_migration_lock``), and they observe the
    winner's rows in schema_migrations.
    """
    import psycopg

    applied_now: list[str] = []
    with psycopg.connect(dsn) as conn:
        _acquire_migration_lock(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version    text PRIMARY KEY,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            conn.commit()
            cur.execute("SELECT version FROM schema_migrations")
            applied = {row[0] for row in cur.fetchall()}

        for path in pending_migrations(applied, migrations_dir):
            if dry_run:
                applied_now.append(path.stem)
                continue
            migration_sql = path.read_text()
            try:
                built = _concurrent_index(migration_sql)
            except ValueError as exc:
                raise ValueError(f"{path.name}: {exc}") from exc
            logger.info("applying migration %s", path.name)
            if built is not None:
                _build_index_concurrently(conn, path.stem, migration_sql, *built)
            else:
                with conn.cursor() as cur:
                    cur.execute(migration_sql)
                    cur.execute(
                        "INSERT INTO schema_migrations (version) VALUES (%s)",
                        (path.stem,),
                    )
                conn.commit()
            applied_now.append(path.stem)

        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_KEY,))
        conn.commit()
    return applied_now


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply Nomos PostgreSQL schema migrations")
    parser.add_argument(
        "--dsn",
        default=os.environ.get("COUNCIL_DATABASE_URL", ""),
        help="PostgreSQL DSN (default: COUNCIL_DATABASE_URL)",
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=DEFAULT_MIGRATIONS_DIR,
        help="Directory of NNNN_*.sql migration files",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List pending migrations without applying them",
    )
    args = parser.parse_args()
    if not args.dsn:
        parser.error("--dsn or COUNCIL_DATABASE_URL is required")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    applied = run_migrations(args.dsn, migrations_dir=args.dir, dry_run=args.dry_run)
    verb = "would apply" if args.dry_run else "applied"
    print(f"{verb} {len(applied)} migration(s): {', '.join(applied) or '(none)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
