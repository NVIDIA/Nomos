"""
PostgreSQL-backed ledger storage for concurrent council replicas.

The append critical section runs inside one database transaction:

    SELECT ... FOR UPDATE on the chain row   (the lock)
    -> read head entry
    -> builder constructs/hashes/signs the next entry (in-transaction)
    -> INSERT entry, UPDATE chain head
    -> COMMIT

Retries with bounded backoff on serialization/lock/connection failures and
fails closed (LedgerAppendError) when the budget is exhausted — no partial
entries, no writ minting. The (chain_id, sequence) primary key makes chain
forks structurally impossible even under a locking bug.

Synchronous psycopg by design: every append call site is synchronous code
that already runs inside thread-offloaded request paths.
"""

from __future__ import annotations

import logging
import os
import random
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .entry import LedgerEntry
from .storage import AtomicLedgerStorage, EntryBuilder, GenesisBuilder, LedgerAppendError

if TYPE_CHECKING:
    from collections.abc import Iterator

    import psycopg
    from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)

_SELECT_COLUMNS = (
    "sequence, entry_type, payload_json, metadata_json, previous_hash, "
    "entry_hash, signatures_json, entry_timestamp_iso"
)


@dataclass(frozen=True)
class LedgerRetryPolicy:
    """Bounded retry for append transactions."""

    max_attempts: int = 5
    base_delay_s: float = 0.05
    max_delay_s: float = 0.8
    total_deadline_s: float = 3.0

    def delay_for(self, attempt: int) -> float:
        backoff = min(self.base_delay_s * (2**attempt), self.max_delay_s)
        jitter: float = random.uniform(0, 0.025)  # noqa: S311 # nosec B311 - jitter, not crypto
        return float(backoff) + jitter


def _retryable_errors() -> tuple[type[Exception], ...]:
    import psycopg
    from psycopg import errors

    return (
        errors.SerializationFailure,
        errors.DeadlockDetected,
        errors.LockNotAvailable,
        errors.UniqueViolation,
        psycopg.OperationalError,
    )


class PostgresLedgerStorage(AtomicLedgerStorage):
    """One canonical hash-linked chain per chain_id, safe for many writers."""

    def __init__(
        self,
        pool: ConnectionPool,
        chain_id: str,
        *,
        council_id: str = "",
        writer_instance_id: str | None = None,
        lock_timeout_ms: int = 2000,
        statement_timeout_ms: int = 5000,
        retry: LedgerRetryPolicy | None = None,
    ):
        self._pool = pool
        self._chain_id = chain_id
        self._council_id = council_id or chain_id.removeprefix("ledger-")
        self._writer_instance_id = (
            writer_instance_id or os.environ.get("HOSTNAME") or f"writer-{uuid.uuid4().hex[:8]}"
        )
        self._lock_timeout_ms = int(lock_timeout_ms)
        self._statement_timeout_ms = int(statement_timeout_ms)
        self._retry = retry or LedgerRetryPolicy()

    # ── AtomicLedgerStorage ────────────────────────────────────────────

    def append_atomic(self, build: EntryBuilder) -> LedgerEntry:
        retryable = _retryable_errors()
        deadline = time.monotonic() + self._retry.total_deadline_s
        last_error: Exception | None = None

        for attempt in range(self._retry.max_attempts):
            try:
                return self._append_once(build)
            except retryable as exc:
                last_error = exc
                delay = self._retry.delay_for(attempt)
                if time.monotonic() + delay >= deadline:
                    break
                logger.warning(
                    "ledger append attempt %d/%d failed (%s); retrying in %.0fms",
                    attempt + 1,
                    self._retry.max_attempts,
                    exc.__class__.__name__,
                    delay * 1000,
                )
                time.sleep(delay)

        raise LedgerAppendError(
            f"ledger append failed after retries for chain {self._chain_id}: {last_error}"
        ) from last_error

    def _append_once(self, build: EntryBuilder) -> LedgerEntry:
        with self._pool.connection() as conn, conn.transaction():
            self._set_local_timeouts(conn)
            row = conn.execute(
                "SELECT current_head_sequence FROM ledger_chains WHERE chain_id = %s FOR UPDATE",
                (self._chain_id,),
            ).fetchone()
            if row is None:
                raise LedgerAppendError(
                    f"chain {self._chain_id} not initialized; call initialize() first"
                )
            head_sequence = row[0]
            head = self._fetch_entry(conn, head_sequence) if head_sequence is not None else None

            entry = build(head)

            self._insert_entry(conn, entry)
            conn.execute(
                "UPDATE ledger_chains SET current_head_sequence = %s, current_head_hash = %s "
                "WHERE chain_id = %s",
                (entry.sequence, str(entry.content_hash), self._chain_id),
            )
        return entry

    def initialize_atomic(self, build_genesis: GenesisBuilder) -> LedgerEntry:
        with self._pool.connection() as conn, conn.transaction():
            self._set_local_timeouts(conn)
            # The chain row may not exist yet, so FOR UPDATE alone cannot
            # serialize first-boot races; the advisory lock can.
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (self._chain_id,),
            )
            conn.execute(
                "INSERT INTO ledger_chains (chain_id, council_id, created_by) "
                "VALUES (%s, %s, %s) ON CONFLICT (chain_id) DO NOTHING",
                (self._chain_id, self._council_id, self._writer_instance_id),
            )
            row = conn.execute(
                "SELECT current_head_sequence FROM ledger_chains WHERE chain_id = %s FOR UPDATE",
                (self._chain_id,),
            ).fetchone()
            if row is not None and row[0] is not None:
                existing = self._fetch_entry(conn, 0)
                if existing is None:
                    raise LedgerAppendError(
                        f"chain {self._chain_id} has a head but no genesis entry"
                    )
                return existing

            genesis = build_genesis()
            self._insert_entry(conn, genesis)
            conn.execute(
                "UPDATE ledger_chains SET current_head_sequence = %s, current_head_hash = %s "
                "WHERE chain_id = %s",
                (genesis.sequence, str(genesis.content_hash), self._chain_id),
            )
        return genesis

    # ── LedgerStorage (read side) ──────────────────────────────────────

    def append(self, entry: LedgerEntry) -> None:
        raise NotImplementedError(
            "PostgresLedgerStorage only supports append_atomic(); the in-process "
            "read-head-then-append pattern would fork the chain under concurrency"
        )

    def get(self, sequence: int) -> LedgerEntry | None:
        with self._pool.connection() as conn:
            return self._fetch_entry(conn, sequence)

    def get_head(self) -> LedgerEntry | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT current_head_sequence FROM ledger_chains WHERE chain_id = %s",
                (self._chain_id,),
            ).fetchone()
            if row is None or row[0] is None:
                return None
            return self._fetch_entry(conn, row[0])

    def length(self) -> int:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT current_head_sequence FROM ledger_chains WHERE chain_id = %s",
                (self._chain_id,),
            ).fetchone()
        if row is None or row[0] is None:
            return 0
        return int(row[0]) + 1

    def iterate(self) -> Iterator[LedgerEntry]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM ledger_entries "  # noqa: S608 # nosec B608 - static cols
                "WHERE chain_id = %s ORDER BY sequence",
                (self._chain_id,),
            ).fetchall()
        for row in rows:
            yield self._row_to_entry(row)

    # ── internals ──────────────────────────────────────────────────────

    def _set_local_timeouts(self, conn: psycopg.Connection) -> None:
        # SET LOCAL does not accept bind parameters; both values are ints
        # validated by config.
        conn.execute(f"SET LOCAL lock_timeout = '{self._lock_timeout_ms}ms'")
        conn.execute(f"SET LOCAL statement_timeout = '{self._statement_timeout_ms}ms'")

    def _insert_entry(self, conn: psycopg.Connection, entry: LedgerEntry) -> None:
        from psycopg.types.json import Json

        data = entry.to_dict()
        conn.execute(
            "INSERT INTO ledger_entries (chain_id, sequence, entry_type, payload_json, "
            "metadata_json, previous_hash, entry_hash, signatures_json, signer_key_id, "
            "writer_instance_id, entry_timestamp_iso) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                self._chain_id,
                entry.sequence,
                data["entry_type"],
                Json(data["payload"]),
                Json(data["metadata"]),
                data["previous_hash"],
                data["content_hash"],
                Json(data["signatures"]),
                entry.signatures[0].signer_id if entry.signatures else None,
                self._writer_instance_id,
                data["timestamp"],
            ),
        )

    def _fetch_entry(self, conn: psycopg.Connection, sequence: int) -> LedgerEntry | None:
        row = conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM ledger_entries "  # noqa: S608 # nosec B608 - static cols
            "WHERE chain_id = %s AND sequence = %s",
            (self._chain_id, sequence),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_entry(row)

    @staticmethod
    def _row_to_entry(row: tuple[Any, ...]) -> LedgerEntry:
        (
            sequence,
            entry_type,
            payload,
            metadata,
            previous_hash,
            entry_hash,
            signatures,
            timestamp_iso,
        ) = row
        return LedgerEntry.from_dict(
            {
                "sequence": sequence,
                "entry_type": entry_type,
                "payload": payload,
                "metadata": metadata,
                "previous_hash": previous_hash,
                "content_hash": entry_hash,
                "signatures": signatures,
                "timestamp": timestamp_iso,
            }
        )
