"""
PostgreSQL-backed read cache for council workflow state (NOMOS-5b, minimal-correct).

Lets multiple council replicas share workflow *status*: any replica can answer a
``check_intent_status`` / ``get_pending_approvals`` query for an intent that was
decided on another replica. Without this the state lived only in
``SecurityCouncil._workflows`` (per-process), so a status request that
load-balances to a different replica returned "Intent not found".

In the deployed surface a whole review (submit → in-process LLM votes → debate →
decide → mint) runs atomically inside one request on one replica, so the live
vote state machine never crosses a replica boundary. This store is therefore a
**write-through read cache**: the single replica handling an intent upserts the
workflow's durable state (last-writer-wins, ``version`` bumped for audit); other
replicas read it. The full audit trail (votes, debate, decision) already lives in
the shared PostgreSQL ledger, so it is not duplicated here.

Synchronous psycopg by design: the council call sites are synchronous and reuse
the ledger ``ConnectionPool`` (``nomos/level_1/ledger/postgres_storage.py``). A
``None`` store (file/redis dev mode) means callers keep their in-memory behavior
unchanged.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    import psycopg
    from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True)
class WorkflowRetryPolicy:
    """Bounded retry for transient connection/serialization failures."""

    max_attempts: int = 4
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
        psycopg.OperationalError,
    )


class WorkflowStoreError(RuntimeError):
    """Raised when a workflow store operation fails after retries."""


class PostgresWorkflowStore:
    """Replica-shared workflow status cache over the (shared) ledger pool."""

    def __init__(
        self,
        pool: ConnectionPool,
        *,
        council_id: str,
        lock_timeout_ms: int = 2000,
        statement_timeout_ms: int = 5000,
        retry: WorkflowRetryPolicy | None = None,
    ):
        self._pool = pool
        self._council_id = council_id
        self._lock_timeout_ms = int(lock_timeout_ms)
        self._statement_timeout_ms = int(statement_timeout_ms)
        self._retry = retry or WorkflowRetryPolicy()

    # ── write-through ───────────────────────────────────────────────────

    def upsert_workflow(
        self,
        intent_id: str,
        workflow_json: dict[str, Any],
        *,
        state: str,
        is_terminal: bool,
        expires_at: datetime | None,
    ) -> None:
        """Persist the workflow's durable state, last-writer-wins.

        Only the replica handling the intent writes a given row (reviews are
        atomic per request), so a plain upsert is race-free; ``version`` is bumped
        purely for audit/observability.
        """
        from psycopg.types.json import Json

        def _op(conn: psycopg.Connection) -> None:
            with conn.transaction():
                self._set_local_timeouts(conn)
                conn.execute(
                    "INSERT INTO council_workflows "
                    "(council_id, intent_id, state, is_terminal, workflow_json, expires_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (council_id, intent_id) DO UPDATE SET "
                    "state = EXCLUDED.state, is_terminal = EXCLUDED.is_terminal, "
                    "workflow_json = EXCLUDED.workflow_json, expires_at = EXCLUDED.expires_at, "
                    "version = council_workflows.version + 1, updated_at = now()",
                    (
                        self._council_id,
                        intent_id,
                        state,
                        is_terminal,
                        Json(workflow_json),
                        expires_at,
                    ),
                )

        self._with_retry(f"upsert workflow {intent_id}", _op)

    # ── reads ───────────────────────────────────────────────────────────

    def get_workflow(self, intent_id: str) -> dict[str, Any] | None:
        def _op(conn: psycopg.Connection) -> dict[str, Any] | None:
            row = conn.execute(
                "SELECT workflow_json FROM council_workflows "
                "WHERE council_id = %s AND intent_id = %s",
                (self._council_id, intent_id),
            ).fetchone()
            return row[0] if row is not None else None

        return self._with_retry(f"get workflow {intent_id}", _op)

    def list_workflows_by_state(self, states: list[str]) -> list[dict[str, Any]]:
        if not states:
            return []

        def _op(conn: psycopg.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT workflow_json FROM council_workflows "
                "WHERE council_id = %s AND state = ANY(%s) ORDER BY updated_at",
                (self._council_id, list(states)),
            ).fetchall()
            return [row[0] for row in rows]

        return self._with_retry("list workflows by state", _op)

    def reap_terminal_before(self, cutoff: datetime) -> int:
        """Delete terminal rows whose ``expires_at`` is past ``cutoff``.

        Idempotent and safe from any replica (a no-op if another replica already
        reaped the row). Bounds table growth for a long-lived council.
        """

        def _op(conn: psycopg.Connection) -> int:
            with conn.transaction():
                self._set_local_timeouts(conn)
                cur = conn.execute(
                    "DELETE FROM council_workflows "
                    "WHERE council_id = %s AND is_terminal = true "
                    "AND expires_at IS NOT NULL AND expires_at < %s",
                    (self._council_id, cutoff),
                )
                return int(cur.rowcount)

        return self._with_retry("reap terminal workflows", _op)

    # ── internals ───────────────────────────────────────────────────────

    def _with_retry(self, what: str, op: Callable[[psycopg.Connection], T]) -> T:
        retryable = _retryable_errors()
        deadline = time.monotonic() + self._retry.total_deadline_s
        last_error: Exception | None = None

        for attempt in range(self._retry.max_attempts):
            try:
                with self._pool.connection() as conn:
                    return op(conn)
            except retryable as exc:
                last_error = exc
                delay = self._retry.delay_for(attempt)
                if time.monotonic() + delay >= deadline:
                    break
                logger.warning(
                    "workflow store %s attempt %d/%d failed (%s); retrying in %.0fms",
                    what,
                    attempt + 1,
                    self._retry.max_attempts,
                    exc.__class__.__name__,
                    delay * 1000,
                )
                time.sleep(delay)

        raise WorkflowStoreError(f"{what} failed after retries: {last_error}") from last_error

    def _set_local_timeouts(self, conn: psycopg.Connection) -> None:
        # SET LOCAL takes no bind params; both values are config-validated ints.
        conn.execute(f"SET LOCAL lock_timeout = '{self._lock_timeout_ms}ms'")
        conn.execute(f"SET LOCAL statement_timeout = '{self._statement_timeout_ms}ms'")
