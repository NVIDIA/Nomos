#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Read-only export of a QUIESCED legacy council's JSONL ledger and Redis state.

Run as a module in the council environment, with its config and Redis credentials:
``python -m scripts.assurance_snapshot_legacy --output /protected/snapshot.json``.
With no --output, stdout contains the sensitive snapshot JSON only; redirect it
to a protected file. No source keys, accounts, tokens, files or services change.

The caller must stop ALL durable-state writers (council and account management).
Two identical reads catch observed changes but are not a substitute for quiescence.
The output is NOT cryptographically authenticated as a whole. Keep it protected;
the importer separately verifies signed ledger records and account HMACs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from nomos.config.settings import load_config
from nomos.store.redis_store import RedisStore
from scripts.assurance_import_legacy import ImportValidationError, _json_loads


async def _keys(client, prefix: str) -> list[str]:
    # Redis SCAN may return the same key more than once even without mutations.
    return sorted({key async for key in client.scan_iter(match=f"{prefix}*")})


async def _expect_type(client, key: str, expected: str) -> None:
    if await client.type(key) != expected:
        raise ImportValidationError("a durable Redis key has an unexpected or changing type")


async def _read_durable(store: RedisStore, prefix: str) -> dict:
    client = store.client
    accounts = []
    account_prefix = f"{prefix}accounts:"
    account_keys = await _keys(client, account_prefix)
    memberships = []
    for key in account_keys:
        suffix = key.removeprefix(account_prefix)
        if suffix.endswith((":perms", ":councils")):
            await _expect_type(client, key, "set")
            memberships.append(suffix.rsplit(":", 1)[0])
            continue
        await _expect_type(client, key, "hash")
        record = await store.get_account(suffix)
        if not record:
            raise ImportValidationError("an account disappeared while exporting")
        accounts.append(
            {
                "agent_id": suffix,
                "record": record,
                "permissions": sorted(await store.get_account_permissions(suffix)),
                "councils": sorted(await store.get_account_councils(suffix)),
            }
        )
    if not set(memberships) <= {account["agent_id"] for account in accounts}:
        raise ImportValidationError("orphan account permission or council membership keys")

    expected_council_agents: dict[str, set[str]] = {}
    for account in accounts:
        for council in account["councils"]:
            expected_council_agents.setdefault(council, set()).add(account["agent_id"])
    actual_council_agents = {}
    council_prefix = f"{prefix}councils:"
    for key in await _keys(client, council_prefix):
        suffix = key.removeprefix(council_prefix)
        if suffix.endswith(":agents"):
            await _expect_type(client, key, "set")
            actual_council_agents[suffix.removesuffix(":agents")] = await client.smembers(key)
    if actual_council_agents != expected_council_agents:
        raise ImportValidationError(
            "council reverse membership index and account memberships disagree"
        )

    token_index = {}
    token_prefix = f"{prefix}token_index:"
    for key in await _keys(client, token_prefix):
        await _expect_type(client, key, "string")
        token_index[key.removeprefix(token_prefix)] = await client.get(key)

    writs, outcomes = {}, {}
    writ_prefix = f"{prefix}writs:"
    for key in await _keys(client, writ_prefix):
        suffix = key.removeprefix(writ_prefix)
        if suffix.endswith(":outcome"):
            await _expect_type(client, key, "hash")
            outcomes[suffix.removesuffix(":outcome")] = await client.hgetall(key)
        else:
            await _expect_type(client, key, "string")
            writs[suffix] = _json_loads(await client.get(key))

    intents = {}
    intent_prefix = f"{prefix}intents:"
    for key in await _keys(client, intent_prefix):
        await _expect_type(client, key, "string")
        intents[key.removeprefix(intent_prefix)] = _json_loads(await client.get(key))

    agent_intents = {}
    queue_prefix = f"{prefix}agent_intents:"
    for key in await _keys(client, queue_prefix):
        await _expect_type(client, key, "list")
        agent_intents[key.removeprefix(queue_prefix)] = await client.lrange(key, 0, -1)

    executors = []
    executor_prefix = f"{prefix}trusted_executors:"
    fingerprints = []
    for key in await _keys(client, executor_prefix):
        await _expect_type(client, key, "hash")
        fingerprint = key.removeprefix(executor_prefix)
        record = await store.get_trusted_executor(fingerprint)
        if not record or record.get("key_fingerprint") != fingerprint:
            raise ImportValidationError("executor registry key and record disagree")
        executors.append(record)
        fingerprints.append(fingerprint)
    if set(fingerprints) != await client.smembers(f"{prefix}trusted_executors"):
        raise ImportValidationError("executor registry index and records disagree")
    return {
        "accounts": accounts,
        "token_index": token_index,
        "writs": writs,
        "writ_outcomes": outcomes,
        "intents": intents,
        "agent_intents": agent_intents,
        "trusted_executors": executors,
    }


async def export_snapshot(
    store: RedisStore, *, prefix: str, council_id: str, ledger_path: Path
) -> dict:
    """Read all durable namespaces twice and refuse an observed source change."""
    before = ledger_path.read_text()
    if not before.endswith("\n"):
        raise ImportValidationError("ledger has no final newline; possible partial append")
    entries = [_json_loads(line) for line in before.splitlines() if line.strip()]
    first = await _read_durable(store, prefix)
    second = await _read_durable(store, prefix)
    if first != second or before != ledger_path.read_text():
        raise ImportValidationError("source changed while exporting; quiesce all writers")
    return {
        "schema_version": 1,
        "ledger": {
            "chain_id": f"ledger-{council_id}",
            "council_id": council_id,
            "entries": entries,
        },
        "redis": first,
    }


async def _run(args) -> dict:
    settings = load_config(str(args.config))
    if settings.storage.mode != "redis":
        raise ImportValidationError("export only supports the legacy Redis/file storage mode")
    store = RedisStore(settings.redis)
    await store.connect()
    try:
        return await export_snapshot(
            store,
            prefix=settings.redis.key_prefix,
            council_id=settings.council_id,
            ledger_path=args.ledger,
        )
    finally:
        await store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/app/dynamic-config/council.yaml"))
    parser.add_argument("--ledger", type=Path, default=Path("/app/ledger/council-ledger.jsonl"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    # Library logging must not emit secret-bearing backend exception messages.
    logging.disable(logging.CRITICAL)
    try:
        snapshot = asyncio.run(_run(args))
        encoded = json.dumps(snapshot, ensure_ascii=False, allow_nan=False) + "\n"
        if args.output:
            descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as output:
                output.write(encoded)
        else:
            sys.stdout.write(encoded)
        return 0
    except Exception as exc:
        detail = str(exc) if isinstance(exc, ImportValidationError) else type(exc).__name__
        print(f"Legacy snapshot export failed: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
