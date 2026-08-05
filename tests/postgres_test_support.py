# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared PostgreSQL test configuration helpers.

Mirrors redis_test_support.py: tests that need a real Postgres skip cleanly
when none is reachable. Point NOMOS_TEST_DATABASE_URL at a disposable
database; tests truncate the nomos tables between runs.
"""

from __future__ import annotations

import os

DEFAULT_TEST_DSN = (
    "postgresql://nomos:nomos-dev@127.0.0.1:5432/nomos_test"  # pragma: allowlist secret
)

# Tables owned by the nomos migrations, in FK-safe truncate order.
NOMOS_TABLES = (
    "ledger_entries",
    "ledger_chains",
    "token_index",
    "account_permissions",
    "account_councils",
    "accounts",
    "writs",
    "writ_outcomes",
    "intents",
    "trusted_executors",
    "council_workflows",
)


def postgres_dsn_for_tests() -> str:
    """Return the DSN integration tests should use."""
    return os.environ.get("NOMOS_TEST_DATABASE_URL", DEFAULT_TEST_DSN)


def postgres_unavailable_reason() -> str | None:
    """Return a pytest skip reason when the configured Postgres cannot be used."""
    try:
        import psycopg
    except ImportError:
        return "psycopg not installed (pip install 'nomos[postgres]')"
    dsn = postgres_dsn_for_tests()
    try:
        with psycopg.connect(dsn, connect_timeout=1) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        return f"Postgres not usable at {dsn.rsplit('@', 1)[-1]}: {exc.__class__.__name__}"
    return None


def ensure_migrated_and_clean(dsn: str) -> None:
    """Apply migrations (idempotent) and truncate all nomos tables."""
    import psycopg

    from nomos.store.migrate import run_migrations

    run_migrations(dsn)
    with psycopg.connect(dsn) as conn:
        conn.execute(f"TRUNCATE {', '.join(NOMOS_TABLES)} CASCADE")
        conn.commit()
