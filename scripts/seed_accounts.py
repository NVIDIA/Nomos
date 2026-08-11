#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Seed agent accounts into Redis for Nomos.

Creates test accounts with different permission tiers.
Tokens are random 32-byte hex strings; only sha256(token) is stored.

Usage:
    python scripts/seed_accounts.py --redis-url redis://:nomos-dev@localhost:6379/0
    python scripts/seed_accounts.py --redis-url redis://:PASSWORD@redis:6379/0 --prefix nomos:
"""

import argparse
import asyncio
import os
import sys
import urllib.parse

DEFAULT_COUNCIL_ID = "council-prod"

# ── Account definitions ──────────────────────────────────────────────

ACCOUNTS = [
    {
        "agent_id": "agent-admin",
        "display_name": "Admin Agent",
        "permissions": [
            "shell:read",
            "shell:write",
            "file:read",
            "file:write",
            "file:delete",
            "infra:deploy",
            "infra:destroy",
        ],
    },
    {
        "agent_id": "agent-developer",
        "display_name": "Developer Agent",
        "permissions": [
            "shell:read",
            "shell:write",
            "file:read",
            "file:write",
        ],
    },
    {
        "agent_id": "agent-readonly",
        "display_name": "Read-Only Agent",
        "permissions": [
            "shell:read",
            "file:read",
        ],
    },
]


async def seed(redis_url: str, prefix: str) -> list[dict]:
    """Seed accounts through the shared provisioning module.

    Writes via nomos.auth.provisioning over a RedisStore rather than raw Redis
    commands. The previous implementation wrote no `_integrity_hmac`, so every
    account it seeded was rejected by any council configured with an account
    integrity key — the accounts existed and looked correct, and authentication
    simply failed.
    """
    from nomos.auth import provisioning
    from nomos.config.settings import RedisConfig
    from nomos.store.redis_store import RedisStore

    parsed = urllib.parse.urlparse(redis_url)
    config = RedisConfig(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        db=int((parsed.path or "").lstrip("/") or 0),
        password=urllib.parse.unquote(parsed.password or ""),
        key_prefix=prefix,
        # Must match the council's key or the seeded accounts will not authenticate.
        account_integrity_key=os.environ.get("NOMOS_ACCOUNT_INTEGRITY_KEY")
        or os.environ.get("COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY", ""),
    )

    store = RedisStore(config)
    try:
        await store.connect()
    except Exception as e:
        print(f"ERROR: Cannot connect to Redis at {redis_url}: {e}", file=sys.stderr)
        sys.exit(1)

    warning = provisioning.require_signed_store(store, storage_mode="redis")
    if warning:
        print(f"WARNING: {warning}", file=sys.stderr)

    results: list[dict] = []
    try:
        for account in ACCOUNTS:
            created = await provisioning.create_account(
                store,
                agent_id=account["agent_id"],
                display_name=account["display_name"],
                permissions=account["permissions"],
                councils=[DEFAULT_COUNCIL_ID],
                force=True,
            )
            results.append(
                {
                    "agent_id": created["agent_id"],
                    "display_name": created["display_name"],
                    "token": created["token"],
                    "permissions": created["permissions"],
                    "councils": created["councils"],
                }
            )
    finally:
        await store.close()

    return results


def print_summary(results: list[dict]) -> None:
    """Print a formatted summary table."""
    print()
    print("=" * 80)
    print("  SEEDED ACCOUNTS")
    print("=" * 80)

    for r in results:
        print()
        print(f"  Agent ID:     {r['agent_id']}")
        print(f"  Display Name: {r['display_name']}")
        print(f"  Token:        {r['token']}")
        print(f"  Token Hash:   {r['token_hash'][:16]}...")
        print(f"  Permissions:  {', '.join(r['permissions'])}")
        print(f"  Councils:     {', '.join(r['councils'])}")
        print(f"  {'─' * 70}")

    print()
    print("  ⚠  Store these tokens securely. They cannot be recovered from Redis.")
    print("=" * 80)
    print()


def main():
    parser = argparse.ArgumentParser(description="Seed agent accounts into Redis")
    parser.add_argument(
        "--redis-url",
        default="redis://:nomos-dev@localhost:6379/0",
        help="Redis connection URL (default: redis://:nomos-dev@localhost:6379/0)",
    )
    parser.add_argument(
        "--prefix",
        default="nomos:",
        help="Key prefix namespace (default: nomos:)",
    )
    args = parser.parse_args()

    results = asyncio.run(seed(args.redis_url, args.prefix))
    print_summary(results)


if __name__ == "__main__":
    main()
