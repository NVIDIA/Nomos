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
NAT function registrations for the Nomos council.

These functions are exposed as MCP tools via `nat mcp serve`.
Each function wraps a council operation with Pydantic I/O schemas.
"""

import asyncio
import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, Field

from nomos.auth.authenticator import AgentAuthenticator, AgentIdentity
from nomos.auth.permissions import categorize_intent
from nomos.auth.writ_factory import WritFactory
from nomos.config.settings import CouncilSettings, LLMBackendConfig
from nomos.crypto.key_manager import CouncilKeyManager
from nomos.crypto.signer_provider import (
    InMemorySigner,
    SignerProvider,
    VaultTransitSigner,
)
from nomos.level_0.types import AgentRole, CouncilMember, Intent
from nomos.level_0.types.intent import (
    INTENT_BLAST_RADIUS_MAX_LENGTH,
    INTENT_CONTEXT_MAX_LENGTH,
    INTENT_DESCRIPTION_MAX_LENGTH,
    INTENT_JUSTIFICATION_MAX_LENGTH,
)
from nomos.level_0.types.packaged_file import PackagedFile
from nomos.level_0.types.writ import VoteSummary
from nomos.level_1.ledger import FileLedgerStorage, LedgerChain, LedgerEntryType
from nomos.level_2.quorum import VoteType
from nomos.level_4.council import CouncilConfig, SecurityCouncil
from nomos.level_4.council.workflow import WorkflowState
from nomos.llm.base import LLMBackend
from nomos.llm.council_agent import (
    ChairmanAgent,
    ChairmanPreAuthDecision,
    CodeReviewAgent,
    CouncilMemberAgent,
    IntentEvaluation,
    create_backend,
    format_code_review_analysis,
    format_code_review_result_json,
    format_debate_context,
)
from nomos.llm.limiter import EndpointLimiter, LimitedBackend, LimiterPool
from nomos.nat.review_audit import build_intent_review_ledger_payload
from nomos.nat.telemetry_sink import RedisTelemetrySink
from nomos.opa import (
    OPAEvaluationError,
    OPAHttpClient,
    OPAPolicyDecision,
    build_intent_filter_input,
)
from nomos.store.base import DurableStore
from nomos.store.postgres_store import PostgresStore
from nomos.store.redis_store import RedisStore
from trusted_executor_contract import ExecutionEvidence, ExecutionResult

logger = logging.getLogger(__name__)

API_VERSION_V1 = "v1"
API_VERSION_V2 = "v2"
SUPPORTED_API_VERSIONS = {API_VERSION_V1, API_VERSION_V2}

# Hard ceiling on debate rounds. ``max_debate_rounds`` can originate from
# request input, so it is clamped to this bound before being used as a loop
# limit to prevent an unbounded loop on hostile input (sonar S6680).
_MAX_DEBATE_ROUNDS = 25


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000


def _is_unique_violation(exc: BaseException) -> bool:
    """True if ``exc`` (or a cause/context in its chain) is a Postgres unique
    violation (SQLSTATE 23505).

    Walks ``__cause__``/``__context__`` because the ledger storage may wrap the
    psycopg error. Matches on sqlstate or class name to avoid a hard psycopg
    import on this module's import path.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if getattr(cur, "sqlstate", None) == "23505":
            return True
        if cur.__class__.__name__ == "UniqueViolation":
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _log_latency(tool: str, stage: str, start: float, *, intent_id: str = "") -> float:
    duration_ms = _elapsed_ms(start)
    logger.info(
        "council_latency tool=%s stage=%s intent=%s ms=%.1f",
        tool,
        stage,
        intent_id or "-",
        duration_ms,
    )
    return duration_ms


def _stage(
    stages: dict[str, float], tool: str, name: str, start: float, *, intent_id: str = ""
) -> float:
    duration_ms = _log_latency(tool, name, start, intent_id=intent_id)
    stages[name] = duration_ms
    return duration_ms


def _truncate_block(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + f"\n... truncated {len(value) - max_chars} chars ..."


class CouncilTelemetryPublisher:
    """Best-effort internal telemetry publisher.

    Fans each record out to two independent best-effort sinks: the legacy HTTP
    POST to the monitor (latency side-channel) and a Redis stream (the
    post-gateway stats path the monitor reads). Either can be disabled; both are
    fire-and-forget and never raise into the request path.
    """

    def __init__(self, settings: CouncilSettings):
        cfg = settings.telemetry
        self._enabled = bool(cfg.enabled and cfg.url and cfg.token)
        self._url = cfg.url
        self._token = cfg.token
        self._timeout_seconds = cfg.timeout_ms / 1000
        self._tasks: set[asyncio.Task[None]] = set()
        prefix = settings.redis.key_prefix
        self._redis_sink = RedisTelemetrySink(
            stream=f"{prefix}{cfg.redis_stream}",
            maxlen=cfg.redis_maxlen,
            enabled=bool(cfg.redis_enabled),
        )

    def attach_redis(self, client: Any) -> None:
        """Wire the council's connected redis client (call after init_stores)."""
        self._redis_sink.attach(client)

    def publish_later(self, payload: dict[str, Any]) -> None:
        if not (self._enabled or self._redis_sink.active):
            return
        try:
            task = asyncio.create_task(self._publish(payload))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except RuntimeError:
            logger.debug("telemetry publish skipped: no running event loop")

    async def _publish(self, payload: dict[str, Any]) -> None:
        # Redis stream sink (best-effort; swallows its own errors).
        await self._redis_sink.publish(payload)
        # Legacy HTTP latency side-channel.
        if self._enabled:
            try:
                await asyncio.to_thread(self._post, payload)
            except Exception as exc:
                logger.debug("council telemetry publish failed: %s", exc)

    def _post(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 - operator-configured internal monitor URL.
            self._url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Council-Internal-Token": self._token,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:  # noqa: S310
                response.read()
        except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError) as exc:
            raise RuntimeError(f"telemetry POST failed: {exc}") from exc


# ── Pydantic I/O schemas for NAT functions ──────────────────────────────


class ProposeIntentInput(BaseModel):
    """Input schema for proposing an intent to the council."""

    api_version: str = Field(default=API_VERSION_V1, description="Council API version")
    auth_token: str = Field(
        default="", description="Agent authentication token (required when Redis is configured)"
    )
    agent_id: str = Field(..., description="ID of the requesting agent")
    council_id: str = Field(
        default="",
        description=(
            "Council to petition. Optional on v1 compatibility tools; required on v2 tools."
        ),
    )
    description: str = Field(
        ...,
        min_length=10,
        max_length=INTENT_DESCRIPTION_MAX_LENGTH,
        description="What you want to do",
    )
    target_resource: str = Field(..., description="The resource to act upon")
    command: str = Field(default="", description="Exact command to execute")
    command_type: str = Field(
        default="shell", description="Type of command: shell, api, sql, python, kubectl, script"
    )
    working_directory: str = Field(default="", description="Directory where command runs")
    target_host: str = Field(default="", description="Target host machine")
    target_environment: str = Field(
        default="", description="Environment: development, staging, production, dr"
    )
    target_cluster: str = Field(default="", description="Kubernetes cluster name")
    target_namespace: str = Field(default="", description="Kubernetes namespace")
    justification: str = Field(
        default="",
        max_length=INTENT_JUSTIFICATION_MAX_LENGTH,
        description="Why this action is needed",
    )
    blast_radius: str = Field(
        default="",
        max_length=INTENT_BLAST_RADIUS_MAX_LENGTH,
        description="Scope of potential impact",
    )
    context: str = Field(
        default="",
        max_length=INTENT_CONTEXT_MAX_LENGTH,
        description="What the agent was doing before this request",
    )
    evidence: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Supporting evidence (log snippets, command outputs, observed state) "
        "rendered in a dedicated Evidence section of the council review prompt",
    )
    certified_evidence: list[ExecutionResult] = Field(
        default_factory=list,
        max_length=20,
        description=(
            "Executor ExecutionResult objects with signed evidence. The council verifies each "
            "signature against a trusted executor registered in Redis before rendering it."
        ),
    )
    packaged_files: list[PackagedFile] = Field(
        default_factory=list,
        max_length=5,
        description="Files to review and package into an execution writ",
    )


class MemberVote(BaseModel):
    """A single council member's vote on an intent."""

    member_id: str = ""
    display_name: str = ""
    decision: str = ""
    reasoning: str = ""
    risk_concerns: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    guidance: str = Field(default="", description="How the request could be improved if denied")
    changed_vote: bool = Field(
        default=False, description="Whether this member changed position during debate"
    )


class ChairmanInfo(BaseModel):
    """Chairman's threshold decision included in output."""

    threshold: str = Field(default="unanimous", description="'quorum' or 'unanimous'")
    reasoning: str = Field(default="", description="Why this threshold was chosen")
    risk_level: str = Field(default="medium", description="'low', 'medium', 'high', or 'critical'")


class DebateRound(BaseModel):
    """Snapshot of member votes for one round of council debate."""

    round_number: int = Field(description="0 = initial vote, 1+ = debate round number")
    votes: list[MemberVote] = Field(
        default_factory=list, description="Each member's vote in this round"
    )


class ProposeIntentOutput(BaseModel):
    """Output schema for propose_intent."""

    api_version: str = API_VERSION_V1
    council_id: str = ""
    success: bool
    intent_id: str = ""
    state: str = ""
    requires_approval: bool = False
    error: str = ""
    retryable: bool = Field(
        default=False,
        description="True when the request was shed due to load and may be retried",
    )
    council_members: list[MemberVote] = Field(
        default_factory=list, description="Final member votes (after all debate rounds)"
    )
    chairman: ChairmanInfo | None = Field(default=None, description="Chairman's threshold decision")
    code_review: MemberVote | None = Field(
        default=None, description="Advisory review of packaged file contents"
    )
    debate_rounds: int = Field(default=0, description="Number of debate rounds that occurred")
    debate_history: list[DebateRound] = Field(
        default_factory=list,
        description="Per-round vote snapshots: index 0 = initial votes, 1+ = debate rounds",
    )


class IntentStatusInput(BaseModel):
    """Input schema for checking intent status."""

    api_version: str = Field(default=API_VERSION_V1, description="Council API version")
    council_id: str = Field(default="", description="Council that owns the intent")
    intent_id: str = Field(..., description="ID of the intent to check")


class IntentStatusOutput(BaseModel):
    """Output schema for check_intent_status."""

    found: bool
    intent_id: str = ""
    state: str = ""
    decision: str = ""
    error: str = ""


class PendingApprovalsOutput(BaseModel):
    """Output schema for get_pending_approvals."""

    council_id: str = ""
    count: int = 0
    intents: list[dict[str, str]] = Field(default_factory=list)
    error: str = ""


class PendingApprovalsInput(BaseModel):
    """Input schema for get_pending_approvals."""

    api_version: str = Field(default=API_VERSION_V1, description="Council API version")
    council_id: str = Field(default="", description="Council whose pending queue to inspect")


class LedgerPageInput(BaseModel):
    """Input schema for council_ledger_page."""

    api_version: str = Field(default=API_VERSION_V1, description="Council API version")
    council_id: str = Field(default="", description="Council whose ledger to read")
    cursor: int | None = Field(
        default=None,
        description="Highest sequence to include (None = chain head); page descends from here",
    )
    limit: int = Field(default=50, ge=1, le=200, description="Maximum entries per page")


class LedgerPageOutput(BaseModel):
    """Output schema for council_ledger_page."""

    council_id: str = ""
    chain_id: str = ""
    head_sequence: int = -1
    entries: list[dict[str, Any]] = Field(
        default_factory=list,
        description="LedgerEntry.to_dict() records, newest first",
    )
    next_cursor: int | None = Field(
        default=None, description="Cursor for the next (older) page; None when exhausted"
    )
    error: str = ""


class LedgerEntryInput(BaseModel):
    """Input schema for council_ledger_get."""

    api_version: str = Field(default=API_VERSION_V1, description="Council API version")
    council_id: str = Field(default="", description="Council whose ledger to read")
    sequence: int = Field(..., ge=0, description="Entry sequence number")


class LedgerEntryOutput(BaseModel):
    """Output schema for council_ledger_get."""

    council_id: str = ""
    chain_id: str = ""
    found: bool = False
    entry: dict[str, Any] | None = None
    error: str = ""


class PublicKeyInput(BaseModel):
    """Input schema for get_council_public_key."""

    api_version: str = Field(default=API_VERSION_V1, description="Council API version")
    council_id: str = Field(default="", description="Council whose public key to retrieve")


class PublicKeyOutput(BaseModel):
    """Output schema for get_council_public_key."""

    council_id: str = ""
    public_key_hex: str = Field(default="", description="Raw 32-byte public key as hex")
    fingerprint: str = Field(default="", description="SHA-256 fingerprint of the public key")
    key_id: str = Field(default="", description="Key identifier")
    error: str = ""


class AuthorizeInput(BaseModel):
    """Input for council_authorize: authenticate → evaluate → mint writ."""

    api_version: str = Field(default=API_VERSION_V1, description="Council API version")
    auth_token: str = Field(default="", description="Agent authentication token")
    agent_id: str = Field(..., description="ID of the requesting agent")
    council_id: str = Field(
        default="",
        description=(
            "Council to petition. Optional on v1 compatibility tools; required on v2 tools."
        ),
    )
    command: str = Field(..., min_length=1, description="Exact command to authorize")
    working_directory: str = Field(
        ..., min_length=1, description="Directory where the command will run (required)"
    )
    env: dict[str, str] = Field(
        default_factory=dict, description="Environment variables for execution"
    )
    description: str = Field(default="", description="What the agent wants to do")
    target_resource: str = Field(default="", description="The resource to act upon")
    justification: str = Field(default="", description="Why this action is needed")
    blast_radius: str = Field(default="", description="Scope of potential impact")
    context: str = Field(default="", description="What the agent was doing before this request")
    evidence: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Supporting evidence (log snippets, command outputs, observed state) "
        "rendered in a dedicated Evidence section of the council review prompt",
    )
    certified_evidence: list[ExecutionResult] = Field(
        default_factory=list,
        max_length=20,
        description=(
            "Executor ExecutionResult objects with signed evidence. The council verifies each "
            "signature against a trusted executor registered in Redis before rendering it."
        ),
    )
    packaged_files: list[PackagedFile] = Field(
        default_factory=list,
        max_length=5,
        description="Files to review and package into the execution writ",
    )


class AuthorizeOutput(BaseModel):
    """Output for council_authorize."""

    api_version: str = API_VERSION_V1
    council_id: str = ""
    status: str = Field(default="denied", description="'approved', 'denied', or 'error'")
    writ: str | None = Field(
        default=None, description="Base64-encoded writ blob (only if approved)"
    )
    writ_id: str = Field(default="", description="Writ ID for status checking")
    decision_id: str = Field(default="", description="Intent/decision ID")
    guidance: list[str] = Field(default_factory=list, description="Guidance from council on denial")
    error: str = Field(default="", description="Error message if status is 'error'")
    retryable: bool = Field(
        default=False,
        description="True when the request was shed due to load and may be retried",
    )
    council_members: list[MemberVote] = Field(default_factory=list, description="Member votes")
    chairman: ChairmanInfo | None = Field(default=None, description="Chairman decision")
    code_review: MemberVote | None = Field(
        default=None, description="Advisory review of packaged file contents"
    )
    missing_permissions: list[str] = Field(
        default_factory=list, description="Permissions the agent lacks"
    )
    debate_rounds: int = Field(default=0, description="Number of debate rounds that occurred")
    debate_history: list[DebateRound] = Field(
        default_factory=list,
        description="Per-round vote snapshots: index 0 = initial votes, 1+ = debate rounds",
    )


class WritStatusInput(BaseModel):
    """Input for check_writ_status."""

    api_version: str = Field(default=API_VERSION_V1, description="Council API version")
    council_id: str = Field(default="", description="Council that issued the writ")
    writ_id: str = Field(..., description="Writ ID to check")
    auth_token: str = Field(default="", description="Agent authentication token")


class WritStatusOutput(BaseModel):
    """Output for check_writ_status."""

    writ_id: str = ""
    status: str = Field(default="unknown", description="'valid', 'expired', 'used', 'not_found'")
    expires_at: float = 0.0
    issued_at: float = 0.0
    command: str = ""
    error: str = ""


class CouncilListInput(BaseModel):
    """Input for listing dynamic council profiles."""

    management_token: str = Field(
        default="", description="Optional management token for deployments that require read auth"
    )


class CouncilShowInput(BaseModel):
    """Input for describing one dynamic council profile."""

    council_id: str = Field(..., min_length=1, description="Council profile to describe")
    management_token: str = Field(
        default="", description="Optional management token for deployments that require read auth"
    )


class CouncilCreateInput(BaseModel):
    """Input for creating a dynamic council from a server-side template."""

    council_id: str = Field(..., min_length=1, description="New council profile id")
    management_token: str = Field(..., min_length=1, description="Council management token")
    template: str = Field(
        default="strict-production",
        description="Server-side profile template to copy",
    )
    description: str = Field(default="", description="Operator-facing description")
    use_case: str = Field(default="", description="Intended use case for this council")
    enabled: bool = Field(
        default=False,
        description="Whether the new council is immediately usable",
    )
    force: bool = Field(default=False, description="Replace an existing generated profile")


class CouncilPutProfileInput(BaseModel):
    """Input for replacing a dynamic council profile with a profile mapping."""

    council_id: str = Field(..., min_length=1, description="Council profile id to create/update")
    management_token: str = Field(..., min_length=1, description="Council management token")
    profile: dict[str, Any] = Field(..., description="Full council profile mapping")
    enabled: bool | None = Field(default=None, description="Optional enabled override")


class CouncilSetEnabledInput(BaseModel):
    """Input for enabling or disabling a dynamic council profile."""

    council_id: str = Field(..., min_length=1, description="Council profile id to toggle")
    management_token: str = Field(..., min_length=1, description="Council management token")
    enabled: bool = Field(..., description="Desired enabled state")


class CouncilCatalogValidateInput(BaseModel):
    """Input for validating the dynamic council catalog."""

    management_token: str = Field(
        default="", description="Optional management token for deployments that require read auth"
    )


class CouncilManagementOutput(BaseModel):
    """Generic output for first-class dynamic council management tools."""

    success: bool
    error: str = ""
    default_council_id: str = ""
    council_id: str = ""
    council: dict[str, Any] | None = None
    councils: list[dict[str, Any]] = Field(default_factory=list)


RequestEvidenceInputT = TypeVar(
    "RequestEvidenceInputT",
    ProposeIntentInput,
    AuthorizeInput,
)


# ── Council Service (holds state, used by NAT functions) ────────────────


class CouncilService:
    """
    Manages the SecurityCouncil instance and LLM-backed council members.

    This is the runtime that NAT functions call into. It:
    1. Holds the SecurityCouncil and council member agents
    2. Orchestrates LLM-based intent evaluation
    3. Translates between Pydantic I/O and council operations
    """

    def __init__(self, settings: CouncilSettings, limiter_pool: LimiterPool | None = None):
        self._settings = settings
        self._limiter_pool = limiter_pool if limiter_pool is not None else LimiterPool()
        self._limiters: list[EndpointLimiter] = []
        # EWMA of measured end-to-end intent durations (seconds), updated from
        # the telemetry 'total' stage. None until the first intent completes;
        # used by _overload_rejection to project a NEW intent's response time.
        self._intent_latency_ewma_s: float | None = None
        # submit_intent mutates ledger sequence numbers and the workflow map
        # and is only safe because calls are serialized; the lock preserves
        # that while the file append runs off the event loop.
        self._submit_lock = asyncio.Lock()
        self._agents: list[CouncilMemberAgent] = []
        self._chairman: ChairmanAgent | None = None
        self._code_reviewer: CodeReviewAgent | None = None
        self._key_manager: CouncilKeyManager | None = None
        self._signer_provider: SignerProvider | None = None
        self._ledger_pool: Any | None = None
        self._workflow_store: Any | None = None
        self._redis_store: RedisStore | None = None
        self._durable_store: DurableStore | None = None
        self._authenticator: AgentAuthenticator | None = None
        self._opa_client: OPAHttpClient | None = (
            OPAHttpClient(settings.opa) if settings.opa.enabled else None
        )
        self._telemetry = CouncilTelemetryPublisher(settings)
        self._redis_configured: bool = bool(settings.redis.host)
        self._init_signer()

        signer = self._active_signer()
        ledger = self._build_ledger(signer)
        # _build_ledger sets self._ledger_pool as a side effect in postgres mode;
        # reuse that pool for the replica-shared workflow status cache (NOMOS-5b).
        self._workflow_store = self._build_workflow_store()

        self._council = SecurityCouncil(
            config=CouncilConfig(
                council_id=settings.council_id,
                require_justification=settings.require_justification,
                default_token_ttl_seconds=settings.default_token_ttl_seconds,
                max_pending_intents=settings.max_pending_intents,
                # Tier-1b: the deployed council writes one consolidated audit record
                # per review instead of ~7 intermediate ledger entries.
                consolidated_review_audit=True,
            ),
            ledger=ledger,
            signer=signer,
            workflow_store=self._workflow_store,
        )
        self._init_agents()
        self._init_code_reviewer()

    def _build_ledger(self, signer: SignerProvider | None) -> LedgerChain | None:
        """Build persistent ledger storage when configured."""
        chain_id = f"ledger-{self._settings.council_id}"
        if self._settings.storage.mode == "postgres":
            return LedgerChain(
                storage=self._build_postgres_ledger_storage(chain_id),
                signer=signer,
                chain_id=chain_id,
            )
        if not self._settings.ledger_path:
            return None
        return LedgerChain(
            storage=FileLedgerStorage(Path(self._settings.ledger_path)),
            signer=signer,
            chain_id=chain_id,
        )

    def _build_postgres_ledger_storage(self, chain_id: str):
        """Create the distributed ledger backend (and apply schema migrations).

        Runs synchronously in the constructor: a council must not come up
        with a half-initialized ledger, and construction happens once per
        council behind the registry lock.
        """
        from psycopg_pool import ConnectionPool

        from nomos.level_1.ledger.postgres_storage import (
            LedgerRetryPolicy,
            PostgresLedgerStorage,
        )

        storage_cfg = self._settings.storage
        if storage_cfg.run_migrations_on_startup:
            from nomos.store.migrate import run_migrations

            run_migrations(storage_cfg.dsn)
        pool = ConnectionPool(
            storage_cfg.dsn,
            min_size=storage_cfg.ledger_pool_min_size,
            max_size=storage_cfg.ledger_pool_max_size,
            timeout=storage_cfg.connect_timeout_s,
            open=True,
        )
        self._ledger_pool = pool
        return PostgresLedgerStorage(
            pool,
            chain_id,
            council_id=self._settings.council_id,
            lock_timeout_ms=storage_cfg.lock_timeout_ms,
            statement_timeout_ms=storage_cfg.statement_timeout_ms,
            retry=LedgerRetryPolicy(max_attempts=max(1, storage_cfg.append_max_retries)),
        )

    def _build_workflow_store(self):
        """Build the replica-shared workflow status cache (NOMOS-5b), if any.

        Returns ``None`` outside postgres mode (file/redis dev mode keeps the
        council's in-memory-only behavior). Reuses the synchronous ledger pool
        built by ``_build_postgres_ledger_storage``.
        """
        if self._settings.storage.mode != "postgres" or self._ledger_pool is None:
            return None

        from nomos.store.workflow_store import PostgresWorkflowStore

        storage_cfg = self._settings.storage
        return PostgresWorkflowStore(
            self._ledger_pool,
            council_id=self._settings.council_id,
            lock_timeout_ms=storage_cfg.lock_timeout_ms,
            statement_timeout_ms=storage_cfg.statement_timeout_ms,
        )

    def _create_limited_backend(self, backend_cfg: LLMBackendConfig) -> LLMBackend:
        """Create a backend behind the shared endpoint limiter and track it."""
        backend = create_backend(backend_cfg, limiter_pool=self._limiter_pool)
        if isinstance(backend, LimitedBackend) and backend.limiter not in self._limiters:
            self._limiters.append(backend.limiter)
        return backend

    def _agent_retry_kwargs(self) -> dict[str, Any]:
        return {
            "max_retries": self._settings.llm_max_retries,
            "retry_budget_seconds": self._settings.llm_retry_budget_seconds,
            "max_retry_after_seconds": self._settings.llm_max_retry_after_seconds,
        }

    def _init_agents(self) -> None:
        """Initialize LLM-backed council member agents from config."""
        for member_cfg in self._settings.members:
            backend_cfg = self._settings.llm_backends.get(member_cfg.backend)
            if backend_cfg is None:
                logger.warning(
                    "Skipping member %s: backend %s not found",
                    member_cfg.id,
                    member_cfg.backend,
                )
                continue

            backend = self._create_limited_backend(backend_cfg)
            agent = CouncilMemberAgent(
                member_config=member_cfg,
                backend=backend,
                temperature=backend_cfg.temperature,
                max_tokens=backend_cfg.max_tokens,
                **self._agent_retry_kwargs(),
            )
            self._agents.append(agent)

            council_member = CouncilMember(
                member_id=member_cfg.id,
                public_key_hex="",
                display_name=member_cfg.display_name or member_cfg.id,
                weight=member_cfg.weight,
            )
            self._council.register_council_member(council_member)

        # Initialize chairman if configured
        chairman_cfg = self._settings.chairman
        if chairman_cfg.enabled and chairman_cfg.backend:
            backend_cfg = self._settings.llm_backends.get(chairman_cfg.backend)
            if backend_cfg:
                backend = self._create_limited_backend(backend_cfg)
                self._chairman = ChairmanAgent(
                    backend=backend,
                    system_prompt=chairman_cfg.system_prompt_override,
                    allow_quorum=chairman_cfg.allow_quorum,
                    temperature=backend_cfg.temperature,
                    max_tokens=backend_cfg.max_tokens,
                    **self._agent_retry_kwargs(),
                )
                logger.info("Chairman initialized (backend=%s)", chairman_cfg.backend)
            else:
                logger.warning(
                    "Chairman backend %s not found — chairman disabled", chairman_cfg.backend
                )

        logger.info("Initialized %d council member agents", len(self._agents))

    def _init_code_reviewer(self) -> None:
        """Initialize the packaged-file code reviewer if configured."""
        cfg = self._settings.code_review
        if not cfg.enabled:
            return
        if not cfg.backend:
            logger.warning("Code review enabled without a backend — skipping")
            return
        backend_cfg = self._settings.llm_backends.get(cfg.backend)
        if backend_cfg is None:
            logger.warning("Code review backend %s not found — skipping", cfg.backend)
            return
        self._code_reviewer = CodeReviewAgent(
            backend=self._create_limited_backend(backend_cfg),
            display_name=cfg.display_name,
            system_prompt=cfg.system_prompt_override,
            max_file_bytes=cfg.max_file_bytes,
            temperature=backend_cfg.temperature,
            max_tokens=max(backend_cfg.max_tokens, 2048),
            **self._agent_retry_kwargs(),
        )
        logger.info("Code reviewer initialized (backend=%s)", cfg.backend)

    def _init_signer(self) -> None:
        """Initialize the council signer (local key files or Vault transit)."""
        signing = self._settings.signing
        if signing.provider == "vault":
            # Private key bytes never reach this process; signing happens in
            # Vault transit. No key manager, no key files, no public export.
            self._signer_provider = VaultTransitSigner(signing, signing.council_key_id)
            return

        # Local provider: preserve the historical on-disk key behavior.
        key_path = self._settings.council_key_path
        if key_path:
            try:
                self._key_manager = CouncilKeyManager(key_path)
                self._key_manager.load_or_generate()
                public_export_path = os.environ.get("COUNCIL_PUBLIC_KEY_EXPORT_PATH", "")
                if public_export_path:
                    self._key_manager.export_public_key(public_export_path)
            except Exception as e:
                logger.warning("Failed to initialize council key manager: %s", e)
                self._key_manager = None

    def _active_signer(self) -> SignerProvider | None:
        """Return the signer for the current configuration.

        Vault mode uses the provider built at init. Local mode derives a
        signer from the key manager on each call — tests inject
        `_key_manager` directly, and deriving fresh keeps signing consistent
        with whatever key pair is currently installed.
        """
        if self._signer_provider is not None:
            return self._signer_provider
        if self._key_manager is not None:
            return InMemorySigner(self._key_manager.key_pair)
        return None

    def _durable(self) -> DurableStore | None:
        """Durable store with redis-mode fallback.

        In redis mode (and in tests that inject `_redis_store` directly) the
        Redis store IS the durable store.
        """
        return self._durable_store if self._durable_store is not None else self._redis_store

    async def init_stores(self) -> None:
        """Initialize stores and the authenticator (call after __init__).

        Redis is connected in every mode — nonces and rate counters live
        there and keep their fail-closed contract. In postgres mode the
        durable store (accounts/writs/intents/executors) is PostgreSQL;
        in redis mode the same RedisStore fills both roles.
        """
        redis_cfg = self._settings.redis
        try:
            self._redis_store = RedisStore(redis_cfg)
            await self._redis_store.connect()
            if await self._redis_store.health_check():
                logger.info("Redis connected at %s:%d", redis_cfg.host, redis_cfg.port)
                # Stats sink shares the council's redis client (fail-soft).
                self._telemetry.attach_redis(self._redis_store.client)
            else:
                logger.warning("Redis health check failed — authentication disabled")
                self._redis_store = None
        except Exception as e:
            logger.warning("Redis not available — authentication disabled: %s", e)
            self._redis_store = None
            self._authenticator = None

        storage_cfg = self._settings.storage
        if storage_cfg.mode == "postgres":
            store = PostgresStore(
                storage_cfg.dsn,
                pool_min_size=storage_cfg.pool_min_size,
                pool_max_size=storage_cfg.pool_max_size,
                connect_timeout_s=storage_cfg.connect_timeout_s,
                account_integrity_key=redis_cfg.account_integrity_key,
            )
            if not await store.connect_with_retry():
                # Fail closed: a postgres-mode council without its durable
                # store must not silently fall back to Redis.
                raise RuntimeError(
                    "storage.mode=postgres but PostgreSQL is unreachable; refusing to start"
                )
            self._durable_store = store
        else:
            self._durable_store = self._redis_store

        if self._redis_store is not None and self._durable_store is not None:
            self._authenticator = AgentAuthenticator(self._durable_store, self._redis_store)
            logger.info(
                "authentication enabled (durable=%s)",
                type(self._durable_store).__name__,
            )

    async def init_redis(self) -> None:
        """Deprecated alias for init_stores()."""
        await self.init_stores()

    @property
    def auth_required(self) -> bool:
        """True when the deployment is configured to fail closed on auth issues."""
        return self._settings.require_auth

    @property
    def auth_available(self) -> bool:
        """True when the authenticator is connected and can validate tokens."""
        return self._authenticator is not None

    @property
    def council_id(self) -> str:
        """Configured council id for this service instance."""
        return self._settings.council_id

    async def _authenticate(self, auth_token: str) -> AgentIdentity | None:
        """Authenticate an agent token. Returns None if auth is disabled or fails."""
        if self._authenticator is None:
            return None
        if not auth_token:
            return None
        identity = await self._authenticator.authenticate(auth_token)
        if identity is None:
            logger.warning(
                "Authentication failed for token %s...",
                auth_token[:8] if len(auth_token) > 8 else "***",
            )
        return identity

    async def _resolve_request_agent(
        self,
        requested_agent_id: str,
        auth_token: str,
    ) -> tuple[AgentIdentity | None, str | None]:
        """Resolve the effective agent identity for an incoming request.

        Both an available authenticator and a valid token are mandatory when
        the deployment is using the default fail-closed authentication posture.
        A valid token always wins over the caller-supplied agent_id so
        authenticated callers cannot spoof another identity by passing a
        different string in the payload.
        """
        if self.auth_required and not self.auth_available:
            return None, "Authentication service unavailable (fail-closed: require_auth is enabled)"

        identity = await self._authenticate(auth_token)
        if self.auth_required and identity is None:
            return None, "Authentication failed or missing token (require_auth is enabled)"

        if (
            identity is not None
            and self.auth_required
            and self._settings.require_council_membership
            and not AgentAuthenticator.can_petition_council(identity, self.council_id)
        ):
            return (
                None,
                f"Agent {identity.agent_id!r} is not a member of council {self.council_id!r}",
            )

        return identity, None

    def _validate_request_metadata(self, api_version: str, council_id: str) -> str | None:
        """Validate API/council routing metadata for this service."""
        if api_version not in SUPPORTED_API_VERSIONS:
            return (
                f"Unsupported api_version {api_version!r}. Supported versions: "
                f"{sorted(SUPPORTED_API_VERSIONS)}"
            )
        if council_id and council_id != self.council_id:
            return (
                f"Request council_id {council_id!r} does not match selected council "
                f"{self.council_id!r}"
            )
        return None

    async def _evaluate_opa_filter(
        self,
        intent: Intent,
        *,
        request_type: str,
        authenticated: bool,
        agent_permissions: set[str],
        agent_councils: set[str],
        required_permissions: set[str],
    ) -> OPAPolicyDecision | None:
        """Evaluate the configured OPA filter, returning None when disabled."""
        if self._opa_client is None:
            return None

        input_doc = build_intent_filter_input(
            intent,
            request_type=request_type,
            authenticated=authenticated,
            opa_config=self._settings.opa,
            agent_permissions=agent_permissions,
            agent_councils=agent_councils,
            required_permissions=required_permissions,
        )
        try:
            return await self._opa_client.evaluate(input_doc)
        except OPAEvaluationError as exc:
            if self._settings.opa.fail_closed:
                return OPAPolicyDecision(
                    allow=False,
                    reasons=[f"OPA evaluation failed: {exc}"],
                    guidance=[
                        "Retry after OPA is healthy, or disable OPA fail-closed only through "
                        "deployment configuration if that is intentional.",
                    ],
                    metadata={"fail_closed": True},
                )
            logger.warning("OPA evaluation failed; allowing due to fail_open config: %s", exc)
            return OPAPolicyDecision(
                allow=True,
                reasons=[f"OPA unavailable; fail-open allowed by deployment config: {exc}"],
                metadata={"fail_closed": False},
            )

    @staticmethod
    def _opa_denial_error(decision: OPAPolicyDecision) -> str:
        reasons = "; ".join(decision.reasons) if decision.reasons else "policy denied the intent"
        return f"Denied by OPA policy: {reasons}"

    @staticmethod
    def _opa_denial_guidance(decision: OPAPolicyDecision) -> list[str]:
        return decision.guidance or decision.reasons

    async def _prepare_request_evidence(
        self,
        input_data: RequestEvidenceInputT,
        *,
        agent_id: str | None = None,
        council_id: str | None = None,
    ) -> tuple[RequestEvidenceInputT, list[dict[str, Any]], list[str], str]:
        """Validate certified evidence and render it for review.

        Returns ``(input_data, certified_summaries, certified_rendered, error)``.
        Raw agent ``evidence`` is left untouched on ``input_data``; verified
        executor-certified evidence is rendered into ``certified_rendered`` — a
        separate channel — so the council never has to infer provenance from
        string content.
        """
        if input_data.evidence and not self._settings.allow_uncertified_evidence:
            return (
                input_data,
                [],
                [],
                "Uncertified evidence is disabled for this council; submit certified_evidence.",
            )

        certified_summaries: list[dict[str, Any]] = []
        certified_rendered: list[str] = []
        evidence_agent_id = agent_id or input_data.agent_id
        evidence_council_id = council_id or input_data.council_id or self.council_id
        for result in input_data.certified_evidence:
            error = await self._validate_certified_evidence_result(
                result,
                agent_id=evidence_agent_id,
                council_id=evidence_council_id,
            )
            if error:
                return input_data, certified_summaries, certified_rendered, error
            evidence = result.evidence
            if evidence is None:
                return (
                    input_data,
                    certified_summaries,
                    certified_rendered,
                    "Certified evidence is missing signature",
                )
            certified_rendered.append(self._render_certified_evidence(result, evidence))
            certified_summaries.append(
                {
                    "executor_id": evidence.payload.executor_id,
                    "executor_key_fingerprint": evidence.key_fingerprint,
                    "writ_id": evidence.payload.writ_id,
                    "command": evidence.payload.command.command,
                    "success": result.success,
                    "exit_code": result.exit_code,
                    "stdout_sha256": evidence.payload.result.stdout_sha256,
                    "stderr_sha256": evidence.payload.result.stderr_sha256,
                }
            )

        return (input_data, certified_summaries, certified_rendered, "")

    async def _validate_certified_evidence_result(
        self,
        result: ExecutionResult,
        *,
        agent_id: str,
        council_id: str,
    ) -> str:
        """Return an error string if executor-certified evidence is not trusted."""
        evidence = result.evidence
        if evidence is None:
            return "Certified evidence item is missing executor evidence"
        if evidence.algorithm.lower() != "ed25519":
            return f"Unsupported evidence signature algorithm: {evidence.algorithm}"
        if not evidence.public_key_hex:
            return "Certified evidence is missing executor public key"
        try:
            public_key_bytes = bytes.fromhex(evidence.public_key_hex)
        except ValueError:
            return "Certified evidence executor public key is not valid hex"
        if len(public_key_bytes) != 32:
            return "Certified evidence executor public key must be 32 bytes"
        expected_fingerprint = hashlib.sha256(public_key_bytes).hexdigest()
        if evidence.key_fingerprint != expected_fingerprint:
            return "Certified evidence key fingerprint does not match public key"
        if evidence.payload.executor_key_fingerprint != evidence.key_fingerprint:
            return "Certified evidence payload key fingerprint mismatch"
        if evidence.payload.council_id != council_id:
            return "Certified evidence council_id does not match request council_id"
        if evidence.payload.target_agent_id != agent_id:
            return "Certified evidence target_agent_id does not match request agent_id"
        if not evidence.verify():
            return "Certified evidence signature verification failed"
        durable = self._durable()
        if durable is None:
            return "Cannot verify certified evidence because Redis is unavailable"
        trusted = await durable.get_trusted_executor(evidence.key_fingerprint)
        if not trusted:
            return f"Certified evidence executor is not registered: {evidence.key_fingerprint}"
        if trusted.get("enabled") != "1":
            return f"Certified evidence executor is disabled: {evidence.key_fingerprint}"
        if trusted.get("public_key_hex", "").lower() != evidence.public_key_hex.lower():
            return "Certified evidence public key does not match registered executor key"
        return self._validate_evidence_payload_matches_result(result, evidence)

    @staticmethod
    def _validate_evidence_payload_matches_result(
        result: ExecutionResult,
        evidence: ExecutionEvidence,
    ) -> str:
        payload = evidence.payload
        if result.executor_id and payload.executor_id and result.executor_id != payload.executor_id:
            return "Certified evidence executor_id does not match result executor_id"
        if result.writ_id and payload.writ_id and result.writ_id != payload.writ_id:
            return "Certified evidence writ_id does not match result writ_id"
        stdout_hash = hashlib.sha256(result.stdout.encode("utf-8", errors="replace")).hexdigest()
        stderr_hash = hashlib.sha256(result.stderr.encode("utf-8", errors="replace")).hexdigest()
        if stdout_hash != payload.result.stdout_sha256:
            return "Certified evidence stdout hash does not match result stdout"
        if stderr_hash != payload.result.stderr_sha256:
            return "Certified evidence stderr hash does not match result stderr"
        if result.success != payload.result.success:
            return "Certified evidence success flag does not match result"
        if result.exit_code != payload.result.exit_code:
            return "Certified evidence exit_code does not match result"
        if result.error != payload.result.error:
            return "Certified evidence error does not match result"
        if result.verification_stage != payload.result.verification_stage:
            return "Certified evidence verification_stage does not match result"
        return ""

    @staticmethod
    def _render_certified_evidence(
        result: ExecutionResult,
        evidence: ExecutionEvidence,
    ) -> str:
        """Render verified executor evidence for the LLM council prompt."""
        payload = evidence.payload
        stdout = _truncate_block(result.stdout, 4000)
        stderr = _truncate_block(result.stderr, 2000)
        attachments = ", ".join(
            f"{item.path} sha256={item.sha256} mode={item.mode}" for item in payload.attachments
        )
        return (
            "CERTIFIED EXECUTOR EVIDENCE "
            f"(signature verified, executor_key={evidence.key_fingerprint[:16]}):\n"
            "verified_by_council: executor Ed25519 signature, registered public key, "
            "signed command/result payload, and stdout/stderr hashes matched\n"
            "trust_assumption: this certifies the named trusted executor returned this "
            "result; it relies on the executor private key remaining secure\n"
            f"executor_id: {payload.executor_id}\n"
            f"writ_id: {payload.writ_id}\n"
            f"council_id: {payload.council_id}\n"
            f"command: {payload.command.command}\n"
            f"working_directory: {payload.command.working_directory}\n"
            f"attachments: {attachments or '(none)'}\n"
            f"success: {result.success}\n"
            f"exit_code: {result.exit_code}\n"
            f"verification_stage: {result.verification_stage}\n"
            f"stdout_sha256: {payload.result.stdout_sha256}\n"
            f"stderr_sha256: {payload.result.stderr_sha256}\n"
            f"stdout:\n{stdout or '(empty)'}\n"
            f"stderr:\n{stderr or '(empty)'}"
        )

    @staticmethod
    def _build_propose_intent_kwargs(
        input_data: ProposeIntentInput, agent_id: str
    ) -> dict[str, Any]:
        """Translate propose_intent input into Intent.create kwargs."""
        intent_kwargs: dict[str, Any] = {
            "agent_id": agent_id,
            "council_id": input_data.council_id,
            "description": input_data.description,
            "target_resource": input_data.target_resource,
            "justification": input_data.justification,
            "blast_radius": input_data.blast_radius,
            "context": input_data.context,
        }
        if input_data.command:
            intent_kwargs["command"] = input_data.command
        if input_data.command_type:
            intent_kwargs["command_type"] = input_data.command_type
        if input_data.working_directory:
            intent_kwargs["working_directory"] = input_data.working_directory
        if input_data.target_host:
            intent_kwargs["target_host"] = input_data.target_host
        if input_data.target_environment:
            intent_kwargs["target_environment"] = input_data.target_environment
        if input_data.target_cluster:
            intent_kwargs["target_cluster"] = input_data.target_cluster
        if input_data.target_namespace:
            intent_kwargs["target_namespace"] = input_data.target_namespace
        if input_data.evidence:
            intent_kwargs["evidence"] = input_data.evidence
        if input_data.packaged_files:
            intent_kwargs["packaged_files"] = input_data.packaged_files
        return intent_kwargs

    @staticmethod
    def _build_authorize_intent_kwargs(input_data: AuthorizeInput, agent_id: str) -> dict[str, Any]:
        """Translate authorize input into Intent.create kwargs."""
        description = input_data.description or f"Execute: {input_data.command[:80]}"
        if len(description) < 10:
            # Pad to the 10-char minimum without deriving a repeat count from
            # user-controlled data (sonar S6680).
            description = description.ljust(10)

        intent_kwargs: dict[str, Any] = {
            "agent_id": agent_id,
            "council_id": input_data.council_id,
            "description": description,
            "target_resource": input_data.target_resource
            or input_data.working_directory
            or "localhost",
            "command": input_data.command,
            "justification": input_data.justification,
            "blast_radius": input_data.blast_radius,
            "context": input_data.context,
        }
        if input_data.working_directory:
            intent_kwargs["working_directory"] = input_data.working_directory
        if input_data.evidence:
            intent_kwargs["evidence"] = input_data.evidence
        if input_data.packaged_files:
            intent_kwargs["packaged_files"] = input_data.packaged_files
        return intent_kwargs

    def _member_vote_from_code_review(self, review: IntentEvaluation) -> MemberVote:
        return MemberVote(
            member_id="code-review",
            display_name=self._code_reviewer.display_name if self._code_reviewer else "Code Review",
            decision=review.decision,
            reasoning=format_code_review_result_json(review),
            risk_concerns=review.risk_concerns,
            conditions=review.conditions,
            guidance=review.guidance,
        )

    @staticmethod
    def _intent_with_code_review(intent: Intent, review: IntentEvaluation) -> Intent:
        analysis = format_code_review_analysis(review)
        context_parts = [intent.context] if intent.context else []
        context_parts.append(analysis)
        metadata = dict(intent.metadata)
        metadata["code_review"] = review.model_dump(mode="json")
        return intent.model_copy(
            update={
                "context": "\n\n".join(context_parts),
                "metadata": metadata,
            }
        )

    async def _run_code_review(self, intent: Intent) -> tuple[Intent, MemberVote | None]:
        """Run the advisory Code Review member before voting members."""
        if not intent.packaged_files or self._code_reviewer is None:
            return intent, None
        review = await self._code_reviewer.review_intent_files(intent)
        return self._intent_with_code_review(intent, review), self._member_vote_from_code_review(
            review
        )

    async def _collect_review_outcome(
        self,
        intent: Intent,
        *,
        enable_debate: bool,
        max_debate_rounds: int,
        actor_timings: list[dict[str, Any]] | None = None,
    ) -> tuple[list[MemberVote], list[DebateRound], int, WorkflowState | None]:
        """Collect member votes and run any configured debate rounds."""
        votes = await self._evaluate_with_agents(
            intent,
            finalize=False,
            actor_timings=actor_timings,
        )
        workflow = self._council.finalize_intent_review(
            intent.intent_id,
            allow_pending=enable_debate and not self._all_agree(votes),
        ).workflow

        debate_history: list[DebateRound] = [DebateRound(round_number=0, votes=list(votes))]
        debate_rounds_completed = 0

        # Clamp the (potentially request-supplied) round count to a safe ceiling
        # before using it as a loop bound (sonar S6680).
        bounded_rounds = max(0, min(int(max_debate_rounds), _MAX_DEBATE_ROUNDS))

        if (
            enable_debate
            and votes
            and workflow is not None
            and workflow.state == WorkflowState.AWAITING_APPROVAL
        ):
            for round_num in range(1, bounded_rounds + 1):
                logger.info(
                    "Intent %s: starting debate round %d (votes: %s)",
                    intent.intent_id,
                    round_num,
                    {v.member_id: v.decision for v in votes},
                )
                votes = await self._run_debate_round(
                    intent,
                    votes,
                    round_num,
                    finalize=False,
                    actor_timings=actor_timings,
                )
                workflow = self._council.finalize_intent_review(
                    intent.intent_id,
                    allow_pending=round_num < bounded_rounds and not self._all_agree(votes),
                ).workflow
                debate_history.append(DebateRound(round_number=round_num, votes=list(votes)))
                debate_rounds_completed += 1
                if workflow is None or workflow.state != WorkflowState.AWAITING_APPROVAL:
                    logger.info(
                        "Intent %s: review resolved after debate round %d (%s)",
                        intent.intent_id,
                        round_num,
                        workflow.state.value if workflow else "missing-workflow",
                    )
                    break

        final_workflow = self._council.get_workflow(intent.intent_id)
        return (
            votes,
            debate_history,
            debate_rounds_completed,
            final_workflow.state if final_workflow else None,
        )

    def _record_consolidated_review(
        self,
        intent: Intent,
        *,
        request_type: str,
        final_state: str,
        decision: str,
        workflow: Any | None = None,
        opa_decision: Any | None = None,
        votes: list[MemberVote] | None = None,
        chairman: ChairmanInfo | None = None,
        code_review: Any | None = None,
        debate_history: list[DebateRound] | None = None,
        denial_reason: str = "",
        timing: dict[str, Any] | None = None,
    ) -> None:
        """Append the single Tier-1b consolidated review record (no-op in legacy mode).

        Called exactly once per accepted intent that reaches a final outcome. The
        intermediate review entries were suppressed in the council/quorum (gated by
        the same ``consolidated_review_audit`` flag), so this is the lone audit row
        for the review. Failures propagate like the legacy DECISION_MADE append —
        this record is the authoritative audit trail, not a best-effort cache.
        """
        if not self._council.config.consolidated_review_audit:
            return

        def _dump(obj: Any) -> dict[str, Any] | None:
            if obj is None:
                return None
            return obj.model_dump() if hasattr(obj, "model_dump") else None

        opa_block = None
        if opa_decision is not None:
            opa_block = {
                "enabled": True,
                "allow": bool(getattr(opa_decision, "allow", True)),
                "reason": "; ".join(getattr(opa_decision, "reasons", []) or []),
            }

        policy_evaluation: dict[str, Any] = {}
        quorum: dict[str, Any] = {}
        approved_at = denied_at = None
        if workflow is not None:
            if workflow.evaluation_result is not None:
                policy_evaluation = {
                    "decision": workflow.evaluation_result.decision.decision.value,
                    "explanation": workflow.evaluation_result.explanation,
                }
            if workflow.vote_record is not None:
                quorum = workflow.vote_record.to_dict()
            approved_at = workflow.approved_at.isoformat() if workflow.approved_at else None
            denied_at = workflow.denied_at.isoformat() if workflow.denied_at else None
            if not denial_reason:
                denial_reason = workflow.denial_reason
        if votes:
            quorum = {**quorum, "member_votes": [v.model_dump() for v in votes]}

        payload = build_intent_review_ledger_payload(
            request_type=request_type,
            intent_dict=intent.to_dict(),
            council_id=self._settings.council_id,
            agent_id=intent.agent_id,
            final_state=final_state,
            decision=decision,
            denial_reason=denial_reason,
            approved_at=approved_at,
            denied_at=denied_at,
            opa=opa_block,
            policy_evaluation=policy_evaluation,
            chairman=_dump(chairman),
            code_review=_dump(code_review),
            quorum=quorum,
            debate_rounds=[d.model_dump() for d in debate_history] if debate_history else None,
            timing=timing,
        )

        try:
            entry = self._council.ledger.append(
                entry_type=LedgerEntryType.INTENT_REVIEW_RECORDED,
                payload=payload,
            )
        except Exception as exc:  # idempotency: a duplicate review must not 500
            if _is_unique_violation(exc):
                # The 0004 partial unique index already holds a review record for
                # this intent (e.g. a replayed intent_id). Treat as already
                # recorded — the audit row exists, the chain is intact.
                logger.info(
                    "consolidated review already recorded for %s; duplicate append skipped",
                    intent.intent_id,
                )
                return
            raise
        if workflow is not None and getattr(entry, "sequence", None) is not None:
            workflow.record_ledger_sequence(entry.sequence)

    @property
    def council(self) -> SecurityCouncil:
        return self._council

    @property
    def agents(self) -> list[CouncilMemberAgent]:
        return self._agents

    @property
    def key_manager(self) -> CouncilKeyManager | None:
        return self._key_manager

    @property
    def redis_store(self) -> RedisStore | None:
        return self._redis_store

    @property
    def authenticator(self) -> AgentAuthenticator | None:
        return self._authenticator

    def _update_intent_latency_ewma(self, total_seconds: float) -> None:
        if self._intent_latency_ewma_s is None:
            self._intent_latency_ewma_s = total_seconds
        else:
            self._intent_latency_ewma_s += 0.2 * (total_seconds - self._intent_latency_ewma_s)

    def _publish_latency(
        self,
        *,
        tool: str,
        intent_id: str,
        outcome: str,
        stages: dict[str, float],
        actors: list[dict[str, Any]],
    ) -> None:
        """Publish sanitized timing telemetry to the monitor side channel."""
        if not intent_id:
            return
        total_ms = stages.get("total")
        if total_ms is not None:
            self._update_intent_latency_ewma(total_ms / 1000.0)
        self._telemetry.publish_later(
            {
                "tool": tool,
                "council_id": self.council_id,
                "intent_id": intent_id,
                "outcome": outcome,
                "stages": {k: round(v, 1) for k, v in stages.items()},
                "actors": actors,
                "limiters": [limiter.gauge() for limiter in self._limiters],
            }
        )

    @staticmethod
    def _actor_latency(
        *,
        role: str,
        member_id: str,
        provider: str,
        duration_ms: float,
        decision: str = "",
        timing: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        entry = {
            "role": role,
            "member_id": member_id,
            "provider": provider,
            "duration_ms": round(duration_ms, 1),
            "decision": decision,
        }
        if timing:
            for key in ("queue_wait_ms", "inference_ms", "attempts"):
                if key in timing:
                    entry[key] = timing[key]
        return entry

    def _overload_rejection(self) -> tuple[str, list[str]] | None:
        """Shed new council work when projected INTENT response time is past the threshold.

        Queue depth never gates by itself. An intent runs several sequential
        LLM stages, each of which is stretched by the endpoint queue factor
        (1 + waiting/max_concurrent), so a new arrival's response time is
        projected as measured intent-e2e EWMA x queue factor. Until the first
        intent completes (cold start) the per-call projection is used instead.
        The measured EWMA already includes whatever queueing existed when its
        samples ran, which makes the projection conservative under sustained
        load and self-correcting as conditions change.
        """
        for limiter in self._limiters:
            threshold = limiter.overload_latency_threshold
            if threshold <= 0:
                continue
            intent_ewma = self._intent_latency_ewma_s
            if intent_ewma is not None:
                # The measured average already reflects whatever queueing its
                # samples experienced (and most queued calls are PARALLEL
                # member fan-out, not work ahead of a new arrival), so it is
                # used as-is: never scaled by queue depth. Scaling by the
                # queue factor double-counted and shed at ~8x real latency.
                projected = intent_ewma
                basis = f"measured_intent_ewma={intent_ewma:.1f}s"
            else:
                # Cold start: no completed intent yet — fall back to the
                # single-call projection, which only trips on a pathological
                # queue (one call alone projecting past the threshold).
                projected = limiter.projected_response_seconds()
                basis = "per-call cold start"
            if projected < threshold:
                continue
            logger.warning(
                "council_overloaded council=%s waiting=%d max_concurrent=%d "
                "intent_response=%.1fs threshold=%.1fs basis=%s",
                self.council_id,
                limiter.waiting,
                limiter.max_concurrent,
                projected,
                threshold,
                basis,
            )
            error = (
                "council_overloaded: average intent response time "
                f"~{projected:.0f}s exceeds the latency threshold "
                f"({threshold:.0f}s)"
            )
            return error, [f"Retry after ~{max(1, round(limiter.estimated_wait_seconds()))}s"]
        return None

    def get_ledger_page(self, input_data: LedgerPageInput) -> LedgerPageOutput:
        """Return a page of ledger entries, newest first.

        This is the monitor's credential-free read path: the monitor calls
        this tool over HTTP instead of mounting ledger files or holding
        database credentials.
        """
        ledger = self._council.ledger
        chain_id = ledger.chain_id
        head = ledger.get_head()
        if head is None:
            return LedgerPageOutput(council_id=self.council_id, chain_id=chain_id)

        start = (
            head.sequence if input_data.cursor is None else min(input_data.cursor, head.sequence)
        )
        entries: list[dict[str, Any]] = []
        sequence = start
        while sequence >= 0 and len(entries) < input_data.limit:
            entry = ledger.get(sequence)
            if entry is not None:
                entries.append(entry.to_dict())
            sequence -= 1
        return LedgerPageOutput(
            council_id=self.council_id,
            chain_id=chain_id,
            head_sequence=head.sequence,
            entries=entries,
            next_cursor=sequence if sequence >= 0 else None,
        )

    def get_ledger_entry(self, input_data: LedgerEntryInput) -> LedgerEntryOutput:
        """Return one ledger entry by sequence."""
        ledger = self._council.ledger
        entry = ledger.get(input_data.sequence)
        return LedgerEntryOutput(
            council_id=self.council_id,
            chain_id=ledger.chain_id,
            found=entry is not None,
            entry=entry.to_dict() if entry is not None else None,
        )

    def get_public_key(self) -> PublicKeyOutput:
        """Return the council's public key info."""
        signer = self._active_signer()
        if signer is None:
            return PublicKeyOutput(error="Council signer not initialized")
        try:
            return PublicKeyOutput(
                council_id=self.council_id,
                public_key_hex=signer.public_key_bytes().hex(),
                fingerprint=signer.fingerprint(),
                key_id=signer.key_id,
            )
        except Exception as e:
            return PublicKeyOutput(error=str(e))

    async def propose_intent(
        self,
        input_data: ProposeIntentInput,
        enable_debate: bool = False,
        max_debate_rounds: int = 1,
    ) -> ProposeIntentOutput:
        """Submit an intent, run LLM evaluations, and collect votes."""
        tool = "council_propose_intent"
        stages: dict[str, float] = {}
        actors: list[dict[str, Any]] = []
        total_start = time.perf_counter()
        stage_start = time.perf_counter()
        if input_data.api_version == API_VERSION_V2 and not input_data.council_id:
            _stage(stages, tool, "total", total_start)
            return ProposeIntentOutput(
                api_version=input_data.api_version,
                council_id="",
                success=False,
                error="council_id is required for v2 council API calls",
            )
        effective_council_id = input_data.council_id or self.council_id
        metadata_error = self._validate_request_metadata(
            input_data.api_version,
            effective_council_id,
        )
        if metadata_error:
            _stage(stages, tool, "total", total_start)
            return ProposeIntentOutput(
                api_version=input_data.api_version,
                council_id=effective_council_id,
                success=False,
                error=metadata_error,
            )
        input_data = input_data.model_copy(update={"council_id": effective_council_id})
        agent_identity, auth_error = await self._resolve_request_agent(
            input_data.agent_id,
            input_data.auth_token,
        )
        _stage(stages, tool, "auth", stage_start)
        if auth_error:
            _stage(stages, tool, "total", total_start)
            return ProposeIntentOutput(
                api_version=input_data.api_version,
                council_id=effective_council_id,
                success=False,
                error=auth_error,
            )

        overload = self._overload_rejection()
        if overload is not None:
            error, _guidance = overload
            _stage(stages, tool, "total", total_start)
            return ProposeIntentOutput(
                api_version=input_data.api_version,
                council_id=effective_council_id,
                success=False,
                error=error,
                retryable=True,
            )

        agent_id = agent_identity.agent_id if agent_identity else input_data.agent_id

        stage_start = time.perf_counter()
        (
            input_data,
            certified_evidence_metadata,
            certified_evidence_rendered,
            evidence_error,
        ) = await self._prepare_request_evidence(
            input_data,
            agent_id=agent_id,
            council_id=effective_council_id,
        )
        _stage(stages, tool, "evidence", stage_start)
        if evidence_error:
            _stage(stages, tool, "total", total_start)
            return ProposeIntentOutput(
                api_version=input_data.api_version,
                council_id=effective_council_id,
                success=False,
                error=evidence_error,
            )

        stage_start = time.perf_counter()
        # Register agent if not already known
        self._council.register_agent(agent_id, AgentRole.APPLICATION)

        # Build intent kwargs. action_type is no longer agent-supplied — the
        # Intent model defaults it, and the command-based categorizer is the
        # primary signal for required permissions.
        intent_kwargs = self._build_propose_intent_kwargs(input_data, agent_id)
        if certified_evidence_metadata:
            intent_kwargs["metadata"] = {"certified_evidence": certified_evidence_metadata}
        if certified_evidence_rendered:
            intent_kwargs["certified_evidence_rendered"] = certified_evidence_rendered

        try:
            intent = Intent.create(**intent_kwargs)
        except Exception as e:
            _stage(stages, tool, "intent_build", stage_start)
            _stage(stages, tool, "total", total_start)
            return ProposeIntentOutput(
                api_version=input_data.api_version,
                council_id=effective_council_id,
                success=False,
                error=f"Invalid intent: {e}",
            )
        _stage(stages, tool, "intent_build", stage_start, intent_id=intent.intent_id)

        stage_start = time.perf_counter()
        agent_permissions = agent_identity.permissions if agent_identity else set()
        agent_councils = agent_identity.councils if agent_identity else set()
        required_perms = {p.value for p in categorize_intent(intent)}
        opa_decision = await self._evaluate_opa_filter(
            intent,
            request_type="propose_intent",
            authenticated=agent_identity is not None,
            agent_permissions=agent_permissions,
            agent_councils=agent_councils,
            required_permissions=required_perms,
        )
        _stage(stages, tool, "opa", stage_start, intent_id=intent.intent_id)
        if opa_decision is not None and not opa_decision.allow:
            _stage(stages, tool, "total", total_start, intent_id=intent.intent_id)
            self._publish_latency(
                tool=tool,
                intent_id=intent.intent_id,
                outcome="denied",
                stages=stages,
                actors=actors,
            )
            self._record_consolidated_review(
                intent,
                request_type="propose",
                final_state=WorkflowState.DENIED.value,
                decision="deny",
                opa_decision=opa_decision,
                denial_reason=self._opa_denial_error(opa_decision),
            )
            return ProposeIntentOutput(
                api_version=input_data.api_version,
                council_id=effective_council_id,
                success=True,
                intent_id=intent.intent_id,
                state=WorkflowState.DENIED.value,
                error=self._opa_denial_error(opa_decision),
            )

        stage_start = time.perf_counter()
        code_review_was_run = bool(intent.packaged_files and self._code_reviewer is not None)
        intent, code_review_vote = await self._run_code_review(intent)
        code_review_ms = _stage(
            stages, tool, "code_review", stage_start, intent_id=intent.intent_id
        )
        if code_review_was_run and code_review_vote is not None and self._code_reviewer is not None:
            actors.append(
                self._actor_latency(
                    role="code_review",
                    member_id=self._code_reviewer.member_id,
                    provider=self._code_reviewer.provider_name,
                    duration_ms=code_review_ms,
                    decision=code_review_vote.decision,
                    timing=getattr(code_review_vote, "timing", None),
                )
            )

        # Chairman decides threshold (if configured)
        chairman_decision = None
        chairman_info = None
        if self._chairman:
            stage_start = time.perf_counter()
            chairman_decision = await self._chairman.decide_threshold(intent)
            chairman_ms = _stage(stages, tool, "chairman", stage_start, intent_id=intent.intent_id)
            actors.append(
                self._actor_latency(
                    role="chairman",
                    member_id="chairman",
                    provider=self._chairman.provider_name,
                    duration_ms=chairman_ms,
                    decision=chairman_decision.threshold,
                    timing=getattr(chairman_decision, "timing", None),
                )
            )
            chairman_info = ChairmanInfo(
                threshold=chairman_decision.threshold,
                reasoning=chairman_decision.reasoning,
                risk_level=chairman_decision.risk_level,
            )

        stage_start = time.perf_counter()
        # Submit to council for policy evaluation. The thread keeps the ledger
        # file append off the event loop; the lock preserves the serialization
        # submit_intent relies on.
        async with self._submit_lock:
            result = await asyncio.to_thread(
                self._council.submit_intent,
                intent,
                review_threshold=chairman_decision.threshold if chairman_decision else None,
            )
        _stage(stages, tool, "submit", stage_start, intent_id=intent.intent_id)

        if not result.success:
            _stage(stages, tool, "total", total_start, intent_id=intent.intent_id)
            self._publish_latency(
                tool=tool,
                intent_id=intent.intent_id,
                outcome="error",
                stages=stages,
                actors=actors,
            )
            # Policy DENY is a final review outcome → one consolidated record.
            # A submission error (missing justification, max-pending) is not an
            # accepted-and-reviewed intent → no audit entry.
            if result.workflow is not None and result.workflow.state == WorkflowState.DENIED:
                self._record_consolidated_review(
                    intent,
                    request_type="propose",
                    final_state=result.workflow.state.value,
                    decision="deny",
                    workflow=result.workflow,
                    opa_decision=opa_decision,
                    chairman=chairman_info,
                    code_review=code_review_vote,
                )
            return ProposeIntentOutput(
                api_version=input_data.api_version,
                council_id=effective_council_id,
                success=False,
                intent_id=intent.intent_id,
                state=result.workflow.state.value if result.workflow else "",
                error=result.error or "",
                code_review=code_review_vote,
            )

        stage_start = time.perf_counter()
        votes, debate_history, debate_rounds_completed, state = await self._collect_review_outcome(
            intent,
            enable_debate=enable_debate,
            max_debate_rounds=max_debate_rounds,
            actor_timings=actors,
        )
        _stage(stages, tool, "review_collect", stage_start, intent_id=intent.intent_id)
        state_value = state.value if state else "unknown"

        _stage(stages, tool, "total", total_start, intent_id=intent.intent_id)
        self._publish_latency(
            tool=tool,
            intent_id=intent.intent_id,
            outcome=state_value,
            stages=stages,
            actors=actors,
        )
        if state in (WorkflowState.APPROVED, WorkflowState.DENIED):
            self._record_consolidated_review(
                intent,
                request_type="propose",
                final_state=state_value,
                decision="approve" if state == WorkflowState.APPROVED else "deny",
                workflow=self._council.get_workflow(intent.intent_id),
                opa_decision=opa_decision,
                votes=votes,
                chairman=chairman_info,
                code_review=code_review_vote,
                debate_history=debate_history,
                timing={"stages_ms": dict(stages), "actors": list(actors)},
            )
        return ProposeIntentOutput(
            api_version=input_data.api_version,
            council_id=effective_council_id,
            success=True,
            intent_id=intent.intent_id,
            state=state_value,
            requires_approval=state == WorkflowState.AWAITING_APPROVAL,
            council_members=votes,
            chairman=chairman_info,
            code_review=code_review_vote,
            debate_rounds=debate_rounds_completed,
            debate_history=debate_history,
        )

    async def _evaluate_with_agents(
        self,
        intent: Intent,
        *,
        finalize: bool = True,
        actor_timings: list[dict[str, Any]] | None = None,
    ) -> list[MemberVote]:
        """Run all council member LLM evaluations in parallel."""
        votes: list[MemberVote] = []
        if not self._agents:
            return votes

        async def _evaluate_one(
            agent: CouncilMemberAgent,
        ) -> tuple[CouncilMemberAgent, IntentEvaluation | Exception, float]:
            started = time.perf_counter()
            try:
                return agent, await agent.evaluate_intent(intent), _elapsed_ms(started)
            except Exception as exc:
                return agent, exc, _elapsed_ms(started)

        evaluations = await asyncio.gather(
            *[_evaluate_one(agent) for agent in self._agents],
        )

        for agent, eval_result, duration_ms in evaluations:
            if isinstance(eval_result, Exception):
                eval_result = IntentEvaluation(
                    decision="deny",
                    reasoning=f"Evaluation error: {eval_result}",
                )
            if actor_timings is not None:
                actor_timings.append(
                    self._actor_latency(
                        role="council_member",
                        member_id=agent.member_id,
                        provider=agent.provider_name,
                        duration_ms=duration_ms,
                        decision=eval_result.decision,
                        timing=getattr(eval_result, "timing", None),
                    )
                )

            votes.append(
                MemberVote(
                    member_id=agent.member_id,
                    display_name=agent.display_name,
                    decision=eval_result.decision,  # type: ignore[union-attr]
                    reasoning=eval_result.reasoning,  # type: ignore[union-attr]
                    risk_concerns=eval_result.risk_concerns,  # type: ignore[union-attr]
                    conditions=eval_result.conditions,  # type: ignore[union-attr]
                    guidance=eval_result.guidance,  # type: ignore[union-attr]
                )
            )

            # Cast the vote in the council
            vote_result = self._council.cast_member_vote(
                intent.intent_id,
                agent.member_id,
                VoteType.APPROVE
                if eval_result.decision == "approve"  # type: ignore[union-attr]
                else VoteType.DENY,
                reason=eval_result.reasoning,  # type: ignore[union-attr]
                finalize=finalize,
            )
            if not vote_result.success:
                logger.warning(
                    "Vote cast failed for %s on %s: %s",
                    agent.member_id,
                    intent.intent_id,
                    vote_result.error,
                )

        return votes

    @staticmethod
    def _all_agree(votes: list[MemberVote]) -> bool:
        """Check if all votes agree (all approve or all deny).

        Returns False for an empty list — no votes cast is not consensus.
        The debate loop guards against empty lists via ``if votes and not _all_agree(votes)``,
        so returning False here is safe and prevents a future fast-path
        "if unanimous, approve" from incorrectly approving when all members failed.
        """
        if not votes:
            return False
        decisions = {v.decision for v in votes}
        return len(decisions) == 1

    @staticmethod
    def _votes_to_dicts(votes: list[MemberVote]) -> list[dict[str, Any]]:
        """Convert MemberVote list to dicts for format_debate_context()."""
        return [
            {
                "member_id": v.member_id,
                "display_name": v.display_name,
                "decision": v.decision,
                "reasoning": v.reasoning,
                "risk_concerns": v.risk_concerns,
            }
            for v in votes
        ]

    async def _run_debate_round(
        self,
        intent: Intent,
        previous_votes: list[MemberVote],
        round_number: int,
        *,
        finalize: bool = True,
        actor_timings: list[dict[str, Any]] | None = None,
    ) -> list[MemberVote]:
        """Run a debate round where each member re-evaluates using its own backend.

        Each agent receives the debate context (all members' previous
        positions) and re-evaluates the intent in parallel, using the same
        LLM it used for the initial evaluation to preserve model diversity.
        """
        vote_dicts = self._votes_to_dicts(previous_votes)
        previous_decisions = {v.member_id: v.decision for v in previous_votes}

        async def _debate_one(
            agent: CouncilMemberAgent,
        ) -> tuple[CouncilMemberAgent, IntentEvaluation | Exception, float]:
            started = time.perf_counter()
            try:
                return (
                    agent,
                    await agent.debate_evaluate(
                        intent,
                        format_debate_context(vote_dicts, agent.member_id, round_number),
                    ),
                    _elapsed_ms(started),
                )
            except Exception as exc:
                return agent, exc, _elapsed_ms(started)

        evaluations = await asyncio.gather(*[_debate_one(agent) for agent in self._agents])

        new_votes: list[MemberVote] = []
        for agent, eval_result, duration_ms in evaluations:
            if isinstance(eval_result, Exception):
                eval_result = IntentEvaluation(
                    decision="deny",
                    reasoning=f"Debate evaluation error: {eval_result}",
                )

            changed = (
                agent.member_id in previous_decisions
                and previous_decisions[agent.member_id] != eval_result.decision  # type: ignore[union-attr]
            )

            new_votes.append(
                MemberVote(
                    member_id=agent.member_id,
                    display_name=agent.display_name,
                    decision=eval_result.decision,  # type: ignore[union-attr]
                    reasoning=eval_result.reasoning,  # type: ignore[union-attr]
                    risk_concerns=eval_result.risk_concerns,  # type: ignore[union-attr]
                    conditions=eval_result.conditions,  # type: ignore[union-attr]
                    guidance=eval_result.guidance,  # type: ignore[union-attr]
                    changed_vote=changed,
                )
            )
            if actor_timings is not None:
                actor_timings.append(
                    self._actor_latency(
                        role=f"council_member_debate_{round_number}",
                        member_id=agent.member_id,
                        provider=agent.provider_name,
                        duration_ms=duration_ms,
                        decision=eval_result.decision,
                        timing=getattr(eval_result, "timing", None),
                    )
                )

            # Record the revised vote in the council and ledger
            vote_result = self._council.revise_vote(
                intent.intent_id,
                agent.member_id,
                vote_type_str=eval_result.decision,  # type: ignore[union-attr]
                reason=eval_result.reasoning,  # type: ignore[union-attr]
                debate_round=round_number,
                finalize=finalize,
            )
            if not vote_result.success:
                logger.warning(
                    "Debate vote failed for %s on %s: %s",
                    agent.member_id,
                    intent.intent_id,
                    vote_result.error,
                )

        return new_votes

    def check_intent_status(self, input_data: IntentStatusInput) -> IntentStatusOutput:
        """Check the current status of an intent."""
        workflow = self._council.get_workflow(input_data.intent_id)
        if workflow is None:
            return IntentStatusOutput(
                found=False,
                intent_id=input_data.intent_id,
                error="Intent not found",
            )

        decision = ""
        if workflow.evaluation_result:
            decision = workflow.evaluation_result.decision.decision.value

        return IntentStatusOutput(
            found=True,
            intent_id=input_data.intent_id,
            state=workflow.state.value,
            decision=decision,
        )

    def get_pending_approvals(self) -> PendingApprovalsOutput:
        """List all intents currently awaiting approval."""
        pending = self._council.get_pending_approvals()
        intents = []
        for wf in pending:
            intents.append(
                {
                    "intent_id": wf.intent.intent_id,
                    "agent_id": wf.intent.agent_id,
                    "council_id": wf.intent.council_id,
                    "action_type": wf.intent.action_type,
                    "description": wf.intent.description,
                }
            )
        return PendingApprovalsOutput(
            council_id=self.council_id,
            count=len(intents),
            intents=intents,
        )

    async def authorize(
        self,
        input_data: AuthorizeInput,
        enable_debate: bool = False,
        max_debate_rounds: int = 1,
    ) -> AuthorizeOutput:
        """
        Full authorization flow: authenticate → categorize → pre-auth → evaluate → mint writ.

        1. Authenticate agent (if Redis configured)
        2. Build Intent from request
        3. Categorize intent → required permissions
        4. Chairman pre-auth (permission gate + risk assessment)
        5. If not authorized → return denial with missing permissions
        6. Members evaluate in parallel
        6b. Debate rounds (if enabled and not unanimous)
        7. Apply threshold
        8. If approved → mint writ → return serialized writ blob
        9. If denied → return denial with guidance
        """
        tool = "council_authorize"
        stages: dict[str, float] = {}
        actors: list[dict[str, Any]] = []
        total_start = time.perf_counter()
        stage_start = time.perf_counter()
        if input_data.api_version == API_VERSION_V2 and not input_data.council_id:
            _stage(stages, tool, "total", total_start)
            return AuthorizeOutput(
                api_version=input_data.api_version,
                council_id="",
                status="error",
                error="council_id is required for v2 council API calls",
            )
        effective_council_id = input_data.council_id or self.council_id
        metadata_error = self._validate_request_metadata(
            input_data.api_version,
            effective_council_id,
        )
        if metadata_error:
            _stage(stages, tool, "total", total_start)
            return AuthorizeOutput(
                api_version=input_data.api_version,
                council_id=effective_council_id,
                status="error",
                error=metadata_error,
            )
        input_data = input_data.model_copy(update={"council_id": effective_council_id})
        agent_identity, auth_error = await self._resolve_request_agent(
            input_data.agent_id,
            input_data.auth_token,
        )
        _stage(stages, tool, "auth", stage_start)
        if auth_error:
            _stage(stages, tool, "total", total_start)
            return AuthorizeOutput(
                api_version=input_data.api_version,
                council_id=effective_council_id,
                status="error",
                error=auth_error,
            )

        overload = self._overload_rejection()
        if overload is not None:
            error, guidance = overload
            _stage(stages, tool, "total", total_start)
            return AuthorizeOutput(
                api_version=input_data.api_version,
                council_id=effective_council_id,
                status="error",
                error=error,
                guidance=guidance,
                retryable=True,
            )

        agent_id = agent_identity.agent_id if agent_identity else input_data.agent_id
        agent_permissions = agent_identity.permissions if agent_identity else set()
        agent_councils = agent_identity.councils if agent_identity else set()

        stage_start = time.perf_counter()
        (
            input_data,
            certified_evidence_metadata,
            certified_evidence_rendered,
            evidence_error,
        ) = await self._prepare_request_evidence(
            input_data,
            agent_id=agent_id,
            council_id=effective_council_id,
        )
        _stage(stages, tool, "evidence", stage_start)
        if evidence_error:
            _stage(stages, tool, "total", total_start)
            return AuthorizeOutput(
                api_version=input_data.api_version,
                council_id=effective_council_id,
                status="error",
                error=evidence_error,
            )

        stage_start = time.perf_counter()
        # Register agent
        self._council.register_agent(agent_id, AgentRole.APPLICATION)

        # 2. Build Intent
        # action_type is no longer agent-supplied — Intent defaults it,
        # and categorize_intent uses the command as the primary signal.
        intent_kwargs = self._build_authorize_intent_kwargs(input_data, agent_id)
        if certified_evidence_metadata:
            intent_kwargs["metadata"] = {"certified_evidence": certified_evidence_metadata}
        if certified_evidence_rendered:
            intent_kwargs["certified_evidence_rendered"] = certified_evidence_rendered

        try:
            intent = Intent.create(**intent_kwargs)
        except Exception as e:
            _stage(stages, tool, "intent_build", stage_start)
            _stage(stages, tool, "total", total_start)
            return AuthorizeOutput(
                api_version=input_data.api_version,
                council_id=effective_council_id,
                status="error",
                error=f"Invalid intent: {e}",
                decision_id="",
            )

        # 3. Categorize intent → required permissions
        required_perms = categorize_intent(intent)
        _stage(stages, tool, "intent_build", stage_start, intent_id=intent.intent_id)
        if agent_identity:
            missing_permissions = sorted(
                p.value for p in required_perms if p.value not in agent_permissions
            )
            if missing_permissions:
                _stage(stages, tool, "total", total_start, intent_id=intent.intent_id)
                self._publish_latency(
                    tool=tool,
                    intent_id=intent.intent_id,
                    outcome="denied",
                    stages=stages,
                    actors=actors,
                )
                return AuthorizeOutput(
                    api_version=input_data.api_version,
                    council_id=effective_council_id,
                    status="denied",
                    decision_id=intent.intent_id,
                    error="Insufficient permissions",
                    missing_permissions=missing_permissions,
                )

        stage_start = time.perf_counter()
        opa_decision = await self._evaluate_opa_filter(
            intent,
            request_type="authorize",
            authenticated=agent_identity is not None,
            agent_permissions=agent_permissions,
            agent_councils=agent_councils,
            required_permissions={p.value for p in required_perms},
        )
        _stage(stages, tool, "opa", stage_start, intent_id=intent.intent_id)
        if opa_decision is not None and not opa_decision.allow:
            _stage(stages, tool, "total", total_start, intent_id=intent.intent_id)
            self._publish_latency(
                tool=tool,
                intent_id=intent.intent_id,
                outcome="denied",
                stages=stages,
                actors=actors,
            )
            self._record_consolidated_review(
                intent,
                request_type="authorize",
                final_state=WorkflowState.DENIED.value,
                decision="deny",
                opa_decision=opa_decision,
                denial_reason=self._opa_denial_error(opa_decision),
            )
            return AuthorizeOutput(
                api_version=input_data.api_version,
                council_id=effective_council_id,
                status="denied",
                decision_id=intent.intent_id,
                error=self._opa_denial_error(opa_decision),
                guidance=self._opa_denial_guidance(opa_decision),
            )

        stage_start = time.perf_counter()
        code_review_was_run = bool(intent.packaged_files and self._code_reviewer is not None)
        intent, code_review_vote = await self._run_code_review(intent)
        code_review_ms = _stage(
            stages, tool, "code_review", stage_start, intent_id=intent.intent_id
        )
        if code_review_was_run and code_review_vote is not None and self._code_reviewer is not None:
            actors.append(
                self._actor_latency(
                    role="code_review",
                    member_id=self._code_reviewer.member_id,
                    provider=self._code_reviewer.provider_name,
                    duration_ms=code_review_ms,
                    decision=code_review_vote.decision,
                    timing=getattr(code_review_vote, "timing", None),
                )
            )

        # 4. Chairman pre-auth (if configured and agent is authenticated)
        chairman_info = None
        pre_auth: ChairmanPreAuthDecision | None = None
        if self._chairman and agent_identity:
            stage_start = time.perf_counter()
            pre_auth = await self._chairman.pre_authorize(
                intent=intent,
                agent_permissions=agent_permissions,
                required_permissions={p.value for p in required_perms},
            )
            chairman_ms = _stage(stages, tool, "chairman", stage_start, intent_id=intent.intent_id)
            actors.append(
                self._actor_latency(
                    role="chairman",
                    member_id="chairman",
                    provider=self._chairman.provider_name,
                    duration_ms=chairman_ms,
                    decision=pre_auth.threshold or "denied",
                    timing=getattr(pre_auth, "timing", None),
                )
            )
            if not pre_auth.authorized:
                _stage(stages, tool, "total", total_start, intent_id=intent.intent_id)
                self._publish_latency(
                    tool=tool,
                    intent_id=intent.intent_id,
                    outcome="denied",
                    stages=stages,
                    actors=actors,
                )
                return AuthorizeOutput(
                    api_version=input_data.api_version,
                    council_id=effective_council_id,
                    status="denied",
                    decision_id=intent.intent_id,
                    error="Insufficient permissions",
                    missing_permissions=pre_auth.missing_permissions,
                    code_review=code_review_vote,
                    chairman=ChairmanInfo(
                        threshold="",
                        reasoning=pre_auth.reasoning,
                        risk_level=pre_auth.risk_level,
                    ),
                )
            chairman_info = ChairmanInfo(
                threshold=pre_auth.threshold or "unanimous",
                reasoning=pre_auth.reasoning,
                risk_level=pre_auth.risk_level,
            )
        elif self._chairman:
            # No auth — use standard chairman threshold decision
            stage_start = time.perf_counter()
            chairman_decision = await self._chairman.decide_threshold(intent)
            chairman_ms = _stage(stages, tool, "chairman", stage_start, intent_id=intent.intent_id)
            actors.append(
                self._actor_latency(
                    role="chairman",
                    member_id="chairman",
                    provider=self._chairman.provider_name,
                    duration_ms=chairman_ms,
                    decision=chairman_decision.threshold,
                    timing=getattr(chairman_decision, "timing", None),
                )
            )
            pre_auth = ChairmanPreAuthDecision(
                authorized=True,
                threshold=chairman_decision.threshold,
                risk_level=chairman_decision.risk_level,
                reasoning=chairman_decision.reasoning,
            )
            chairman_info = ChairmanInfo(
                threshold=chairman_decision.threshold,
                reasoning=chairman_decision.reasoning,
                risk_level=chairman_decision.risk_level,
            )

        stage_start = time.perf_counter()
        # Submit to council — policy engine runs here (DENY blocks LLM
        # evaluation). The thread keeps the ledger file append off the event
        # loop; the lock preserves the serialization submit_intent relies on.
        async with self._submit_lock:
            result = await asyncio.to_thread(
                self._council.submit_intent,
                intent,
                review_threshold=pre_auth.threshold if pre_auth and pre_auth.threshold else None,
            )
        _stage(stages, tool, "submit", stage_start, intent_id=intent.intent_id)
        if not result.success:
            # Distinguish a deterministic policy DENY from a submission error
            from nomos.level_1.policy.rules import DecisionType

            workflow = result.workflow
            if (
                workflow is not None
                and workflow.evaluation_result is not None
                and workflow.evaluation_result.decision.decision == DecisionType.DENY
            ):
                _stage(stages, tool, "total", total_start, intent_id=intent.intent_id)
                self._publish_latency(
                    tool=tool,
                    intent_id=intent.intent_id,
                    outcome="denied",
                    stages=stages,
                    actors=actors,
                )
                self._record_consolidated_review(
                    intent,
                    request_type="authorize",
                    final_state=workflow.state.value,
                    decision="deny",
                    workflow=workflow,
                    opa_decision=opa_decision,
                    chairman=chairman_info,
                    code_review=code_review_vote,
                )
                return AuthorizeOutput(
                    api_version=input_data.api_version,
                    council_id=effective_council_id,
                    status="denied",
                    decision_id=intent.intent_id,
                    error=workflow.denial_reason or result.error or "Denied by policy",
                    code_review=code_review_vote,
                )
            _stage(stages, tool, "total", total_start, intent_id=intent.intent_id)
            self._publish_latency(
                tool=tool,
                intent_id=intent.intent_id,
                outcome="error",
                stages=stages,
                actors=actors,
            )
            return AuthorizeOutput(
                api_version=input_data.api_version,
                council_id=effective_council_id,
                status="error",
                decision_id=intent.intent_id,
                error=result.error or "Council submission failed",
                code_review=code_review_vote,
            )

        stage_start = time.perf_counter()
        votes, debate_history, debate_rounds_completed, state = await self._collect_review_outcome(
            intent,
            enable_debate=enable_debate,
            max_debate_rounds=max_debate_rounds,
            actor_timings=actors,
        )
        _stage(stages, tool, "review_collect", stage_start, intent_id=intent.intent_id)
        approved = state == WorkflowState.APPROVED

        if not approved:
            guidance_parts = [v.guidance for v in votes if v.guidance]
            # Record denial in Redis
            durable = self._durable()
            if durable is not None:
                try:
                    await durable.store_intent(
                        intent.intent_id,
                        {
                            "intent_id": intent.intent_id,
                            "command": input_data.command,
                            "agent_id": input_data.agent_id,
                            "decision": "denied",
                            "decided_at": __import__("time").time(),
                        },
                    )
                except Exception:  # noqa: S110
                    pass

            _stage(stages, tool, "total", total_start, intent_id=intent.intent_id)
            self._publish_latency(
                tool=tool,
                intent_id=intent.intent_id,
                outcome="denied",
                stages=stages,
                actors=actors,
            )
            if state == WorkflowState.DENIED:
                self._record_consolidated_review(
                    intent,
                    request_type="authorize",
                    final_state=state.value,
                    decision="deny",
                    workflow=self._council.get_workflow(intent.intent_id),
                    opa_decision=opa_decision,
                    votes=votes,
                    chairman=chairman_info,
                    code_review=code_review_vote,
                    debate_history=debate_history,
                    timing={"stages_ms": dict(stages), "actors": list(actors)},
                )
            return AuthorizeOutput(
                api_version=input_data.api_version,
                council_id=effective_council_id,
                status="denied",
                decision_id=intent.intent_id,
                guidance=guidance_parts,
                council_members=votes,
                chairman=chairman_info,
                code_review=code_review_vote,
                debate_rounds=debate_rounds_completed,
            )

        # 8. Approved → record the consolidated review BEFORE minting the writ, so
        # the audit entry exists even if minting fails (design: append before mint).
        self._record_consolidated_review(
            intent,
            request_type="authorize",
            final_state=WorkflowState.APPROVED.value,
            decision="approve",
            workflow=self._council.get_workflow(intent.intent_id),
            opa_decision=opa_decision,
            votes=votes,
            chairman=chairman_info,
            code_review=code_review_vote,
            debate_history=debate_history,
            timing={"stages_ms": dict(stages), "actors": list(actors)},
        )

        # Guard on the active signer, not the key manager: in vault mode the
        # signer is the VaultTransitSigner and _key_manager is intentionally None.
        if self._active_signer() is None:
            _stage(stages, tool, "total", total_start, intent_id=intent.intent_id)
            self._publish_latency(
                tool=tool,
                intent_id=intent.intent_id,
                outcome="error",
                stages=stages,
                actors=actors,
            )
            return AuthorizeOutput(
                api_version=input_data.api_version,
                council_id=effective_council_id,
                status="error",
                decision_id=intent.intent_id,
                error="Council signer not initialized — cannot mint writ",
                council_members=votes,
                chairman=chairman_info,
                code_review=code_review_vote,
            )

        risk_level = pre_auth.risk_level if pre_auth else "medium"
        member_vote_summaries = [
            VoteSummary(
                member_id=v.member_id,
                decision=v.decision,
                risk_concerns=v.risk_concerns,
            )
            for v in votes
        ]

        factory = WritFactory(
            signer=self._active_signer(),
            council_id=self._settings.council_id,
            store=self._durable(),
        )

        stage_start = time.perf_counter()
        writ = await factory.mint_writ(
            intent=intent,
            target_agent_id=agent_id,
            threshold_used=pre_auth.threshold if pre_auth and pre_auth.threshold else "unanimous",
            member_votes=member_vote_summaries,
            chairman_risk_level=risk_level,
            working_directory=input_data.working_directory,
        )
        workflow = self._council.get_workflow(intent.intent_id)
        if workflow is not None:
            workflow.metadata["decision_expires_at"] = writ.expires_at
            workflow.metadata["writ_id"] = writ.writ_id

        writ_blob = WritFactory.serialize_writ(writ)
        _stage(stages, tool, "mint_writ", stage_start, intent_id=intent.intent_id)

        _stage(stages, tool, "total", total_start, intent_id=intent.intent_id)
        self._publish_latency(
            tool=tool,
            intent_id=intent.intent_id,
            outcome="approved",
            stages=stages,
            actors=actors,
        )
        return AuthorizeOutput(
            api_version=input_data.api_version,
            council_id=effective_council_id,
            status="approved",
            writ=writ_blob,
            writ_id=writ.writ_id,
            decision_id=intent.intent_id,
            council_members=votes,
            chairman=chairman_info,
            code_review=code_review_vote,
            debate_rounds=debate_rounds_completed,
        )

    async def check_writ_status(self, input_data: WritStatusInput) -> WritStatusOutput:
        """Check the current status of a writ."""
        agent_identity, auth_error = await self._resolve_request_agent("", input_data.auth_token)
        if auth_error:
            return WritStatusOutput(
                writ_id=input_data.writ_id,
                status="error",
                error=auth_error,
            )
        if self.auth_available and agent_identity is None:
            return WritStatusOutput(
                writ_id=input_data.writ_id,
                status="error",
                error="Authentication failed or missing token",
            )

        durable = self._durable()
        if durable is None:
            return WritStatusOutput(
                writ_id=input_data.writ_id,
                status="error",
                error="Storage not available",
            )

        writ_data = await durable.get_writ(input_data.writ_id)
        if writ_data is None:
            return WritStatusOutput(
                writ_id=input_data.writ_id,
                status="not_found",
            )

        import time as _time

        expires_at = writ_data.get("expires_at", 0)
        issued_at = writ_data.get("issued_at", 0)
        command = writ_data.get("command", "")
        nonce = writ_data.get("nonce", "")
        target_agent_id = writ_data.get("target_agent_id", "")

        if (
            agent_identity is not None
            and target_agent_id
            and agent_identity.agent_id != target_agent_id
        ):
            return WritStatusOutput(
                writ_id=input_data.writ_id,
                status="error",
                error="Not authorized to inspect this writ",
            )

        # Check if nonce has been claimed (writ used)
        nonce_used = False
        if nonce and self._redis_store is not None:
            nonce_used = await self._redis_store.is_nonce_used(nonce)

        # Check outcome
        outcome = await durable.get_writ_outcome(input_data.writ_id)

        if outcome or nonce_used:
            status = "used"
        elif isinstance(expires_at, int | float) and _time.time() > expires_at:
            status = "expired"
        else:
            status = "valid"

        return WritStatusOutput(
            writ_id=input_data.writ_id,
            status=status,
            expires_at=float(expires_at) if expires_at else 0.0,
            issued_at=float(issued_at) if issued_at else 0.0,
            command=command,
        )
