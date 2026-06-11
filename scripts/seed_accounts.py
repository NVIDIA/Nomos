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
import hashlib
import secrets
import sys

import redis.asyncio as aioredis

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
    """Seed accounts into Redis. Returns list of {agent_id, token, permissions}."""
    client = aioredis.from_url(redis_url, decode_responses=True)

    try:
        await client.ping()
    except Exception as e:
        print(f"ERROR: Cannot connect to Redis at {redis_url}: {e}", file=sys.stderr)
        sys.exit(1)

    results = []

    for account in ACCOUNTS:
        agent_id = account["agent_id"]
        display_name = account["display_name"]
        permissions = account["permissions"]
        councils = [DEFAULT_COUNCIL_ID]

        # Generate random 32-byte token
        raw_token = secrets.token_hex(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        # Store account hash
        account_key = f"{prefix}accounts:{agent_id}"
        await client.hset(
            account_key,
            mapping={
                "token_hash": token_hash,
                "display_name": display_name,
                "created_at": str(__import__("time").time()),
                "enabled": "1",
            },
        )

        # Store permissions
        perms_key = f"{prefix}accounts:{agent_id}:perms"
        await client.delete(perms_key)
        if permissions:
            await client.sadd(perms_key, *permissions)

        councils_key = f"{prefix}accounts:{agent_id}:councils"
        await client.delete(councils_key)
        await client.sadd(councils_key, *councils)
        for council_id in councils:
            await client.sadd(f"{prefix}councils:{council_id}:agents", agent_id)

        # Store reverse index: token_hash → agent_id
        index_key = f"{prefix}token_index:{token_hash}"
        await client.set(index_key, agent_id)

        results.append(
            {
                "agent_id": agent_id,
                "display_name": display_name,
                "token": raw_token,
                "token_hash": token_hash,
                "permissions": permissions,
                "councils": councils,
            }
        )

    await client.aclose()
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
