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
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MIGRATIONS_DIR = Path(__file__).parent / "migrations"
# Arbitrary stable 64-bit key ("NOMO") for the migration advisory lock.
MIGRATION_LOCK_KEY = 0x4E4F4D4F
NO_TRANSACTION_MARKER = "-- migrate: no-transaction"
_CONCURRENT_INDEX = re.compile(
    r"\ACREATE\s+(?:UNIQUE\s+)?INDEX\s+CONCURRENTLY\s+IF\s+NOT\s+EXISTS\s+(\w+)\s[^;]*;?\Z",
    re.IGNORECASE,
)


def pending_migrations(
    applied: set[str], migrations_dir: Path = DEFAULT_MIGRATIONS_DIR
) -> list[Path]:
    """Return unapplied migration files in lexicographic (version) order."""
    files = sorted(p for p in migrations_dir.glob("[0-9]*.sql") if p.is_file())
    return [p for p in files if p.stem not in applied]


def concurrent_index_name(migration_sql: str) -> str | None:
    """Index built by a no-transaction migration; None for an ordinary migration.

    Raises ValueError when the file is marked no-transaction but is anything
    other than one ``CREATE INDEX CONCURRENTLY IF NOT EXISTS`` statement.
    """
    lines = migration_sql.splitlines()
    if not any(line.strip() == NO_TRANSACTION_MARKER for line in lines):
        return None
    statement = "\n".join(line for line in lines if not line.lstrip().startswith("--")).strip()
    match = _CONCURRENT_INDEX.match(statement)
    if match is None:
        raise ValueError(
            "a no-transaction migration must be exactly one "
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS statement"
        )
    return match.group(1)


def _build_index_concurrently(conn: Any, version: str, migration_sql: str, index: str) -> None:
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
            cur.execute(
                """
                SELECT 1 FROM pg_index i
                  JOIN pg_class c ON c.oid = i.indexrelid
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE c.relname = %s AND n.nspname = current_schema() AND NOT i.indisvalid
                """,
                (index,),
            )
            if cur.fetchone() is not None:
                logger.warning("dropping invalid index %s left by an interrupted build", index)
                cur.execute(
                    sql.SQL("DROP INDEX CONCURRENTLY IF EXISTS {}").format(sql.Identifier(index))
                )
            cur.execute(migration_sql)
            cur.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
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
    makes losers wait, and they observe the winner's rows in schema_migrations.
    """
    import psycopg

    applied_now: list[str] = []
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
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
                index = concurrent_index_name(migration_sql)
            except ValueError as exc:
                raise ValueError(f"{path.name}: {exc}") from exc
            logger.info("applying migration %s", path.name)
            if index is not None:
                _build_index_concurrently(conn, path.stem, migration_sql, index)
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
