#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Import a quiesced legacy snapshot into an EMPTY, migrated Nomos PostgreSQL DB.

Run ``python -m scripts.assurance_import_legacy --snapshot SNAPSHOT.json
--trusted-keys PUBLIC_KEYS.json --dry-run`` before the actual import. Credentials
come from COUNCIL_DATABASE_URL and COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY only.

Snapshot v1 has ``ledger: {chain_id, council_id, entries}`` and ``redis`` with
accounts [{agent_id, record, permissions, councils}], token_index {hash: agent},
writs/intents {id: decoded JSON}, writ_outcomes {id: string fields},
agent_intents {agent: [intent IDs newest first]}, and trusted_executors [string fields].
Every collection is required. The caller must
stop source writers before exporting and keep them stopped through cutover.

This preserves legacy signatures; unsigned ledger metadata stays unsigned.
Executor registration and non-account Redis records have no snapshot signature;
their authenticity relies on the operator's protected, directly exported file.
Ephemeral Redis state and in-flight workflows are deliberately not imported.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from nomos.auth.account_integrity import ACCOUNT_INTEGRITY_FIELD, verify_account_record
from nomos.level_0.crypto import ContentHasher, Verifier
from nomos.level_1.ledger.entry import LedgerEntry

TABLES = (
    "accounts",
    "token_index",
    "account_permissions",
    "account_councils",
    "writs",
    "writ_outcomes",
    "intents",
    "trusted_executors",
    "ledger_chains",
    "ledger_entries",
    "council_workflows",
)
ACCOUNT_COLUMNS = {
    "token_hash",
    "display_name",
    "enabled",
    "role",
    "created_at",
    "updated_at",
    ACCOUNT_INTEGRITY_FIELD,
}


class ImportValidationError(ValueError):
    """The source or destination does not meet the one-shot import contract."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AccountSnapshot(_StrictModel):
    agent_id: str
    record: dict[str, str]
    permissions: list[str]
    councils: list[str]


class LedgerSnapshot(_StrictModel):
    chain_id: str
    council_id: str
    entries: list[dict[str, Any]]


class RedisSnapshot(_StrictModel):
    accounts: list[AccountSnapshot]
    token_index: dict[str, str]
    writs: dict[str, dict[str, Any]]
    writ_outcomes: dict[str, dict[str, str]]
    intents: dict[str, dict[str, Any]]
    agent_intents: dict[str, list[str]]
    trusted_executors: list[dict[str, str]]


class Snapshot(_StrictModel):
    schema_version: Literal[1]
    ledger: LedgerSnapshot
    redis: RedisSnapshot


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ImportValidationError("snapshot contains duplicate JSON object keys")
        result[key] = value
    return result


def _reject_constant(value):
    raise ImportValidationError("snapshot contains non-finite JSON numbers")


def _json_loads(text: str):
    try:
        return json.loads(text, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)
    except (TypeError, ValueError) as exc:
        raise ImportValidationError("invalid or ambiguous snapshot JSON") from exc


def read_snapshot(path: Path) -> dict:
    """Read JSON without silently discarding duplicate keys or accepting NaN."""
    result = _json_loads(path.read_text())
    if not isinstance(result, dict):
        raise ImportValidationError("snapshot JSON must be an object")
    return result


def _unique_nonempty(values: list[str], label: str) -> None:
    if any(not value for value in values) or len(set(values)) != len(values):
        raise ImportValidationError(f"duplicate or empty {label}")


def _verify_ledger(ledger: LedgerSnapshot, trusted_keys: dict[str, str]) -> None:
    if not ledger.council_id or ledger.chain_id != f"ledger-{ledger.council_id}":
        raise ImportValidationError("ledger chain does not match its council")
    if not ledger.entries:
        raise ImportValidationError("snapshot must contain a signed genesis")
    verifier = Verifier()
    try:
        for key_id, public_hex in trusted_keys.items():
            public = bytes.fromhex(public_hex)
            if len(public) != 32:
                raise ValueError("invalid key length")
            verifier.register_public_key(key_id, public)
        previous = None
        reviewed_intents = set()
        for sequence, raw in enumerate(ledger.entries):
            entry = LedgerEntry.from_dict(raw)
            if type(entry.sequence) is not int or entry.sequence != sequence:
                raise ValueError("non-contiguous sequence")
            # No normalization of signed timestamps, missing fields or unknown fields.
            if entry.to_dict() != raw or entry.timestamp.tzinfo is None:
                raise ValueError("entry cannot round-trip unchanged")
            if raw["previous_hash"] != previous:
                raise ValueError("broken chain link")
            if sequence == 0 and entry.payload.get("chain_id") != ledger.chain_id:
                raise ValueError("wrong genesis")
            expected = ContentHasher().hash_json(entry.content_for_hashing())
            if expected != entry.content_hash or not entry.signatures:
                raise ValueError("invalid hash or unsigned entry")
            if any(
                sig.algorithm != "ed25519" or not verifier.verify(expected.to_bytes(), sig)
                for sig in entry.signatures
            ):
                raise ValueError("untrusted signature")
            if raw["entry_type"] == "intent_review_recorded":
                intent_id = entry.payload.get("intent_id")
                if not isinstance(intent_id, str) or not intent_id or intent_id in reviewed_intents:
                    raise ValueError("invalid or duplicate review intent")
                reviewed_intents.add(intent_id)
            previous = raw["content_hash"]
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ImportValidationError(
            "ledger signature, identity or continuity validation failed"
        ) from exc


def _verify_durable(state: RedisSnapshot, integrity_key: str) -> None:
    if not integrity_key:
        raise ImportValidationError("account integrity key is required")
    _unique_nonempty([account.agent_id for account in state.accounts], "account IDs")
    expected_index = {}
    for account in state.accounts:
        _unique_nonempty(account.permissions, "permissions")
        _unique_nonempty(account.councils, "council memberships")
        record = account.record
        required = {"token_hash", "display_name", "enabled", "created_at", ACCOUNT_INTEGRITY_FIELD}
        if not required <= record.keys() or record["enabled"] not in {"0", "1"}:
            raise ImportValidationError("account record cannot round-trip unchanged")
        token_hash = record["token_hash"]
        if not token_hash or token_hash in expected_index:
            raise ImportValidationError("empty or shared account token hash")
        expected_index[token_hash] = account.agent_id
        if not verify_account_record(
            key=integrity_key,
            agent_id=account.agent_id,
            account=record,
            permissions=set(account.permissions),
            councils=set(account.councils),
        ):
            raise ImportValidationError("account integrity verification failed")
    if state.token_index != expected_index:
        raise ImportValidationError("token index does not exactly match account identities")

    for label, records in (("writ", state.writs), ("intent", state.intents)):
        _unique_nonempty(list(records), f"{label} IDs")
        for identifier, record in records.items():
            if f"{label}_id" in record and record[f"{label}_id"] != identifier:
                raise ImportValidationError(f"{label} key disagrees with record identity")
    _verify_intent_queues(state)
    for identifier, outcome in state.writ_outcomes.items():
        required = {"exit_code", "stdout_hash", "stderr_hash", "executed_at", "executor_id"}
        if not identifier or set(outcome) != required:
            raise ImportValidationError("invalid execution outcome fields")
        try:
            code = int(outcome["exit_code"])
            if str(code) != outcome["exit_code"] or not -(2**31) <= code < 2**31:
                raise ValueError("invalid integer")
        except ValueError as exc:
            raise ImportValidationError("invalid execution outcome exit code") from exc

    fingerprints = []
    required = {
        "key_fingerprint",
        "executor_id",
        "public_key_hex",
        "enabled",
        "registered_at",
        "updated_at",
        "metadata_json",
    }
    for executor in state.trusted_executors:
        if set(executor) != required or executor["enabled"] not in {"0", "1"}:
            raise ImportValidationError("invalid trusted executor fields")
        try:
            public = bytes.fromhex(executor["public_key_hex"])
            if (
                len(public) != 32
                or hashlib.sha256(public).hexdigest() != executor["key_fingerprint"]
            ):
                raise ValueError("fingerprint mismatch")
        except ValueError as exc:
            raise ImportValidationError("trusted executor key fingerprint mismatch") from exc
        if not isinstance(_json_loads(executor["metadata_json"]), dict):
            raise ImportValidationError("trusted executor metadata must be an object")
        fingerprints.append(executor["key_fingerprint"])
    _unique_nonempty(fingerprints, "executor fingerprints")


def _verify_intent_queues(state: RedisSnapshot) -> None:
    grouped: dict[str, set[str]] = {}
    for identifier, record in state.intents.items():
        agent = record.get("agent_id")
        if agent is not None and not isinstance(agent, str):
            raise ImportValidationError("intent agent identity must be a string")
        if agent:
            grouped.setdefault(agent, set()).add(identifier)
    _unique_nonempty(list(state.agent_intents), "intent queue agent IDs")
    for agent, queue in state.agent_intents.items():
        _unique_nonempty(queue, "queued intent IDs")
        if len(queue) > 30 or not set(queue) <= grouped.get(agent, set()):
            raise ImportValidationError(
                "intent queue has missing or incorrectly attributed records"
            )
    for agent, identifiers in grouped.items():
        queue = state.agent_intents.get(agent, [])
        # SQL retains older intents, while Redis only lists its last 30 IDs.
        # A shorter incomplete list would change the default API result after import.
        if len(queue) != 30 and set(queue) != identifiers:
            raise ImportValidationError(
                "incomplete recent-intent queue cannot preserve query results"
            )


def _intent_time(record: dict) -> float:
    for field in ("decided_at", "created_at"):
        value = record.get(field)
        try:
            timestamp = float(value)
        except (TypeError, ValueError):
            try:
                timestamp = datetime.fromisoformat(str(value)).timestamp()
            except (TypeError, ValueError, OverflowError):
                continue
        if math.isfinite(timestamp):
            return timestamp
    return 0.0


def _intent_order_times(state: RedisSnapshot) -> dict[str, datetime]:
    """Reconstruct query ordering without changing any stored intent JSON.

    Redis has no SQL row-created timestamp. Assign migration-time ordering
    metadata: queued IDs first, then older unqueued IDs sorted by source time.
    All values precede import time so a real subsequent write sorts ahead.
    """
    base = datetime.now(UTC) - timedelta(seconds=1)
    queue_ids = {identifier for queue in state.agent_intents.values() for identifier in queue}
    remaining = sorted(
        set(state.intents) - queue_ids,
        key=lambda identifier: (_intent_time(state.intents[identifier]), identifier),
        reverse=True,
    )
    ordered = [identifier for queue in state.agent_intents.values() for identifier in queue]
    ordered.extend(remaining)
    return {
        identifier: base - timedelta(microseconds=rank) for rank, identifier in enumerate(ordered)
    }


def validate_snapshot(
    data: dict, *, trusted_keys: dict[str, str], account_integrity_key: str
) -> Snapshot:
    """Verify source shape, all ledger signatures and account HMACs without a DB."""
    try:
        # The API accepts decoded JSON too; reject non-finite/non-JSON objects here.
        json.dumps(data, allow_nan=False)
        snapshot = Snapshot.model_validate(data)
    except (ValidationError, TypeError, ValueError) as exc:
        raise ImportValidationError("invalid snapshot structure") from exc
    _verify_ledger(snapshot.ledger, trusted_keys)
    _verify_durable(snapshot.redis, account_integrity_key)
    return snapshot


def _summary(snapshot: Snapshot) -> dict[str, int]:
    state = snapshot.redis
    return {
        "ledger_entries": len(snapshot.ledger.entries),
        "accounts": len(state.accounts),
        "token_index": len(state.token_index),
        "permissions": sum(len(account.permissions) for account in state.accounts),
        "council_memberships": sum(len(account.councils) for account in state.accounts),
        "writs": len(state.writs),
        "writ_outcomes": len(state.writ_outcomes),
        "intents": len(state.intents),
        "agent_intent_queues": len(state.agent_intents),
        "trusted_executors": len(state.trusted_executors),
    }


def _insert_durable(conn, state: RedisSnapshot) -> None:
    from psycopg.types.json import Jsonb

    for account in state.accounts:
        record = account.record
        conn.execute(
            "INSERT INTO accounts (agent_id, token_hash, display_name, enabled, role, "
            "created_at, updated_at, integrity_hmac, extra) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                account.agent_id,
                record["token_hash"],
                record["display_name"],
                record["enabled"] == "1",
                record.get("role"),
                record["created_at"],
                record.get("updated_at"),
                record[ACCOUNT_INTEGRITY_FIELD],
                Jsonb({key: value for key, value in record.items() if key not in ACCOUNT_COLUMNS}),
            ),
        )
        for permission in account.permissions:
            conn.execute(
                "INSERT INTO account_permissions VALUES (%s,%s)", (account.agent_id, permission)
            )
        for council in account.councils:
            conn.execute("INSERT INTO account_councils VALUES (%s,%s)", (account.agent_id, council))
    for token_hash, agent_id in state.token_index.items():
        conn.execute("INSERT INTO token_index VALUES (%s,%s)", (token_hash, agent_id))
    for identifier, record in state.writs.items():
        conn.execute(
            "INSERT INTO writs (writ_id,writ_json) VALUES (%s,%s)", (identifier, Jsonb(record))
        )
    intent_times = _intent_order_times(state)
    for identifier, record in state.intents.items():
        conn.execute(
            "INSERT INTO intents (intent_id,agent_id,intent_json,created_at) VALUES (%s,%s,%s,%s)",
            (identifier, record.get("agent_id"), Jsonb(record), intent_times[identifier]),
        )
    for identifier, record in state.writ_outcomes.items():
        conn.execute(
            "INSERT INTO writ_outcomes (writ_id,exit_code,stdout_hash,stderr_hash,executor_id,executed_at) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (
                identifier,
                int(record["exit_code"]),
                record["stdout_hash"],
                record["stderr_hash"],
                record["executor_id"],
                record["executed_at"],
            ),
        )
    for record in state.trusted_executors:
        conn.execute(
            "INSERT INTO trusted_executors (key_fingerprint,executor_id,public_key_hex,enabled,"
            "registered_at,updated_at,metadata_json) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (
                record["key_fingerprint"],
                record["executor_id"],
                record["public_key_hex"],
                record["enabled"] == "1",
                record["registered_at"],
                record["updated_at"],
                Jsonb(_json_loads(record["metadata_json"])),
            ),
        )


def _insert_ledger(conn, ledger: LedgerSnapshot) -> None:
    from psycopg.types.json import Jsonb

    head = ledger.entries[-1]
    conn.execute(
        "INSERT INTO ledger_chains (chain_id,council_id,created_by,current_head_sequence,"
        "current_head_hash) VALUES (%s,%s,%s,%s,%s)",
        (
            ledger.chain_id,
            ledger.council_id,
            "legacy-import",
            head["sequence"],
            head["content_hash"],
        ),
    )
    for entry in ledger.entries:
        conn.execute(
            "INSERT INTO ledger_entries (chain_id,sequence,entry_type,payload_json,metadata_json,"
            "previous_hash,entry_hash,signatures_json,signer_key_id,writer_instance_id,"
            "entry_timestamp_iso) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                ledger.chain_id,
                entry["sequence"],
                entry["entry_type"],
                Jsonb(entry["payload"]),
                Jsonb(entry["metadata"]),
                entry["previous_hash"],
                entry["content_hash"],
                Jsonb(entry["signatures"]),
                entry["signatures"][0]["signer_id"],
                "legacy-import",
                entry["timestamp"],
            ),
        )


def import_snapshot(
    data: dict, dsn: str, *, trusted_keys: dict[str, str], account_integrity_key: str
) -> dict[str, int]:
    """Validate first, lock every target table, then import atomically or roll back."""
    import psycopg
    from psycopg import sql

    snapshot = validate_snapshot(
        data, trusted_keys=trusted_keys, account_integrity_key=account_integrity_key
    )
    with psycopg.connect(dsn, connect_timeout=5) as conn, conn.transaction():
        conn.execute("SET LOCAL lock_timeout = '5s'")
        conn.execute("SET LOCAL statement_timeout = '60s'")
        # Emptiness and writes share an exclusive table lock: racing initializers
        # cannot slip a genesis or account into the target after the empty check.
        conn.execute(
            sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(
                sql.SQL(",").join(sql.Identifier(table) for table in TABLES)
            )
        )
        for table in TABLES:
            row = conn.execute(
                sql.SQL("SELECT EXISTS (SELECT 1 FROM {})").format(sql.Identifier(table))
            ).fetchone()
            if row[0]:
                raise ImportValidationError("all target durable and ledger tables must be empty")
        _insert_ledger(conn, snapshot.ledger)
        _insert_durable(conn, snapshot.redis)
    return _summary(snapshot)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--trusted-keys", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        data = read_snapshot(args.snapshot)
        keys = read_snapshot(args.trusted_keys)
        integrity_key = os.environ.get("COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY", "")
        if args.dry_run:
            report = _summary(
                validate_snapshot(data, trusted_keys=keys, account_integrity_key=integrity_key)
            )
        else:
            dsn = os.environ.get("COUNCIL_DATABASE_URL", "")
            if not dsn:
                raise ImportValidationError("COUNCIL_DATABASE_URL is required for import")
            report = import_snapshot(
                data, dsn, trusted_keys=keys, account_integrity_key=integrity_key
            )
        print(json.dumps({"validated": True, "imported": not args.dry_run, "counts": report}))
        return 0
    except Exception as exc:  # No database DSNs, snapshot contents, or credentials in diagnostics.
        detail = str(exc) if isinstance(exc, ImportValidationError) else type(exc).__name__
        print(json.dumps({"validated": False, "imported": False, "error": detail}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
