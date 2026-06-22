# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


"""
PostgreSQL schema migration runner.

Deliberately minimal — no ORM, no alembic. Migrations are ordered SQL files
(``NNNN_name.sql``) shipped as package data under ``nomos/store/migrations/``.
Applied versions are tracked in a ``schema_migrations`` table; a session-level
advisory lock serializes replicas that race to migrate at startup.

Usage:
    python -m nomos.store.migrate --dsn postgresql://...   # or COUNCIL_DATABASE_URL
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_MIGRATIONS_DIR = Path(__file__).parent / "migrations"
# Arbitrary stable 64-bit key ("NOMO") for the migration advisory lock.
MIGRATION_LOCK_KEY = 0x4E4F4D4F


def pending_migrations(
    applied: set[str], migrations_dir: Path = DEFAULT_MIGRATIONS_DIR
) -> list[Path]:
    """Return unapplied migration files in lexicographic (version) order."""
    files = sorted(p for p in migrations_dir.glob("[0-9]*.sql") if p.is_file())
    return [p for p in files if p.stem not in applied]


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
            with conn.cursor() as cur:
                logger.info("applying migration %s", path.name)
                cur.execute(path.read_text())
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
