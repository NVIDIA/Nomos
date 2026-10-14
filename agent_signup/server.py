# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Small local service for creating Nomos agent accounts."""

# ruff: noqa: E501
# The embedded HTML/CSS/JS template keeps browser-facing markup readable here.

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import secrets
import time
from base64 import b64decode
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Response, status
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from nomos.auth import provisioning
from nomos.auth.account_integrity import ACCOUNT_INTEGRITY_FIELD, sign_account_record
from nomos.auth.permissions import ROLE_PERMISSIONS, PermissionScope
from nomos.auth.permissions import permissions_for_role as _pkg_permissions_for_role
from nomos_runtime import (
    ClientTLSConfig,
    SecretCheck,
    strict_transport_profile,
    validate_deployment_preflight,
)
from nomos_runtime.redis_client import RedisConnectionConfig, create_redis_client

logger = logging.getLogger(__name__)


def _log_signup_failure(what: str, agent_id: str, exc: BaseException) -> None:
    """Record a signup failure server-side.

    The client gets a generic message: provisioning and driver errors describe
    internal state, and this endpoint is reachable by anyone who can reach the
    console. The detail belongs in the operator's logs, not the HTTP response.
    """
    logger.error("%s for agent_id=%s: %s", what, agent_id, exc, exc_info=exc)


async def _rollback_redis_account(
    client: Any, settings: SignupSettings, agent_id: str, token_hash: str, council_id: str
) -> None:
    """Undo a just-created Redis account whose token will never be returned.

    Revokes reachability first: the token index is what find_agent_by_token_hash
    consults, so removing it before the record means an interrupted rollback
    leaves something unusable rather than something half-authenticating.
    """
    try:
        pipe = client.pipeline()
        pipe.delete(token_index_key(settings.redis_prefix, token_hash))
        pipe.delete(account_key(settings.redis_prefix, agent_id))
        pipe.delete(perms_key(settings.redis_prefix, agent_id))
        pipe.delete(account_councils_key(settings.redis_prefix, agent_id))
        pipe.srem(council_agents_key(settings.redis_prefix, council_id), agent_id)
        await pipe.execute()
    except Exception as exc:  # pragma: no cover - best effort
        _log_signup_failure(
            "ORPHANED ACCOUNT: rollback after a failed signup did not complete; "
            "the account may still authenticate and its token was never returned",
            agent_id,
            exc,
        )


AGENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
COUNCIL_ID_RE = re.compile(r"^[A-Za-z0-9_*.-]+$")
# executor ids follow the build_executor_id convention "{type}:{fp16}", e.g.
# "openshell:2e363bc682be3b5e"; also allow plain ids. Mirrors
# scripts/agent_accounts.py::EXECUTOR_ID_RE.
EXECUTOR_ID_RE = re.compile(r"^[A-Za-z0-9_:.-]+$")

ALL_PERMISSIONS = tuple(scope.value for scope in PermissionScope)


@dataclass(frozen=True)
class SignupSettings:
    redis_url: str
    redis_prefix: str
    default_council_id: str
    allowed_councils: tuple[str, ...]
    default_role: str
    allowed_roles: tuple[str, ...]
    council_endpoint: str
    admin_user: str
    admin_password: str
    council_admin_user: str = "council-admin"
    council_admin_password: str = ""
    storage_mode: str = "redis"
    database_url: str = ""
    account_integrity_key: str = ""
    redis_mode: str = "standalone"
    redis_password: str = ""
    redis_sentinel_service_name: str = ""
    redis_sentinel_nodes: tuple[str, ...] = ()
    redis_sentinel_password: str = ""
    redis_connect_timeout_ms: int = 1000
    redis_command_timeout_ms: int = 1000


class SignupRequest(BaseModel):
    agent_id: Annotated[
        str,
        Field(min_length=1, max_length=80, description="Unique Nomos agent id"),
    ]
    display_name: Annotated[str, Field(max_length=120)] = ""
    council_id: Annotated[str, Field(max_length=120)] = ""
    role: str = ""
    has_trusted_executor: bool = False
    te_mode: str = ""
    te_public_key_hex: Annotated[str, Field(max_length=200)] = ""
    te_executor_id: Annotated[str, Field(max_length=120)] = ""

    @field_validator("agent_id")
    @classmethod
    def validate_agent_id(cls, value: str) -> str:
        if not AGENT_ID_RE.fullmatch(value):
            raise ValueError("agent_id must contain only letters, numbers, hyphen, or underscore")
        return value

    @field_validator("council_id")
    @classmethod
    def validate_council_id(cls, value: str) -> str:
        if value and not COUNCIL_ID_RE.fullmatch(value):
            raise ValueError(
                "council_id must contain only letters, numbers, hyphen, underscore, dot, or '*'"
            )
        return value

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        if value and value not in ROLE_PERMISSIONS:
            raise ValueError(f"role must be one of: {', '.join(sorted(ROLE_PERMISSIONS))}")
        return value

    @field_validator("te_mode")
    @classmethod
    def validate_te_mode(cls, value: str) -> str:
        if value and value not in {"paste", "generate"}:
            raise ValueError("te_mode must be 'paste' or 'generate'")
        return value

    @field_validator("te_executor_id")
    @classmethod
    def validate_te_executor_id(cls, value: str) -> str:
        if value and not EXECUTOR_ID_RE.fullmatch(value):
            raise ValueError("te_executor_id may contain only letters, numbers, _ : . -")
        return value


class SignupResponse(BaseModel):
    agent_id: str
    display_name: str
    council_endpoint: str
    council_id: str
    role: str
    permissions: list[str]
    auth_token: str
    # Trusted executor — only populated when has_trusted_executor. The private
    # key is returned exactly once (generate path) and never persisted.
    te_executor_id: str = ""
    te_public_key_hex: str = ""
    te_key_fingerprint: str = ""
    te_private_key_hex: str = ""


class SkillRequest(BaseModel):
    agent_id: Annotated[str, Field(min_length=1, max_length=80)]
    council_id: Annotated[str, Field(max_length=120)] = ""
    council_endpoint: Annotated[str, Field(max_length=400)] = ""
    mode: str = "both"
    has_trusted_executor: bool = False

    @field_validator("agent_id")
    @classmethod
    def validate_agent_id(cls, value: str) -> str:
        if not AGENT_ID_RE.fullmatch(value):
            raise ValueError("agent_id must contain only letters, numbers, hyphen, or underscore")
        return value

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, value: str) -> str:
        if value not in {"tool-use", "mcp", "both"}:
            raise ValueError("mode must be 'tool-use', 'mcp', or 'both'")
        return value


class AgentAccountRecord(BaseModel):
    agent_id: str
    display_name: str
    enabled: bool
    role: str
    permissions: list[str]
    councils: list[str]
    council_endpoint: str
    created_at: str
    updated_at: str
    token_hash_prefix: str
    auth_token: str = ""


class SignupConfig(BaseModel):
    default_council_id: str
    allowed_councils: list[str]


class SignupConfigUpdate(BaseModel):
    default_council_id: Annotated[str, Field(max_length=120)] = "council-prod"
    allowed_councils: list[Annotated[str, Field(max_length=120)]]

    @field_validator("default_council_id")
    @classmethod
    def validate_default_council_id(cls, value: str) -> str:
        if not COUNCIL_ID_RE.fullmatch(value):
            raise ValueError(
                "default_council_id must contain only letters, numbers, "
                "hyphen, underscore, dot, or '*'"
            )
        return value

    @field_validator("allowed_councils")
    @classmethod
    def validate_allowed_councils(cls, value: list[str]) -> list[str]:
        councils = [council_id.strip() for council_id in value if council_id.strip()]
        if not councils:
            raise ValueError("allowed_councils must include at least one council id")
        for council_id in councils:
            if not COUNCIL_ID_RE.fullmatch(council_id):
                raise ValueError(
                    "allowed_councils must contain only letters, numbers, "
                    "hyphen, underscore, dot, or '*'"
                )
        return list(dict.fromkeys(councils))

    def to_config(self) -> SignupConfig:
        if self.default_council_id not in self.allowed_councils:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="default_council_id must be included in allowed_councils",
            )
        return SignupConfig(
            default_council_id=self.default_council_id,
            allowed_councils=self.allowed_councils,
        )


class AccountListResponse(BaseModel):
    accounts: list[AgentAccountRecord]


class AccountUpdateRequest(BaseModel):
    display_name: Annotated[str | None, Field(max_length=120)] = None
    enabled: bool | None = None
    role: str | None = None
    councils: list[Annotated[str, Field(max_length=120)]] | None = None

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str | None) -> str | None:
        if value is not None and value not in ROLE_PERMISSIONS:
            raise ValueError(f"role must be one of: {', '.join(sorted(ROLE_PERMISSIONS))}")
        return value

    @field_validator("councils")
    @classmethod
    def validate_councils(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        for council_id in value:
            if not COUNCIL_ID_RE.fullmatch(council_id):
                raise ValueError(
                    "councils must contain only letters, numbers, hyphen, underscore, dot, or '*'"
                )
        return value


class TokenRotateResponse(BaseModel):
    account: AgentAccountRecord
    auth_token: str


class HealthResponse(BaseModel):
    ok: bool
    redis: str


def settings_from_env() -> SignupSettings:
    redis_url = os.environ.get(
        "NOMOS_SIGNUP_REDIS_URL",
        os.environ.get("NOMOS_REDIS_URL", "redis://:nomos-dev@localhost:6379/0"),
    )
    allowed_roles = parse_allowed_roles(
        os.environ.get("NOMOS_SIGNUP_ALLOWED_ROLES", "readonly,developer")
    )
    default_council_id = os.environ.get("NOMOS_SIGNUP_COUNCIL_ID", "council-prod")
    allowed_councils = parse_allowed_councils(
        os.environ.get("NOMOS_SIGNUP_ALLOWED_COUNCILS", default_council_id)
    )
    default_role = os.environ.get("NOMOS_SIGNUP_DEFAULT_ROLE", "developer")
    settings = SignupSettings(
        redis_mode=os.environ.get(
            "NOMOS_SIGNUP_REDIS_MODE",
            os.environ.get("NOMOS_REDIS_MODE", "standalone"),
        ),
        redis_url=redis_url,
        redis_prefix=os.environ.get("NOMOS_SIGNUP_REDIS_PREFIX", "nomos:"),
        redis_password=os.environ.get(
            "NOMOS_SIGNUP_REDIS_PASSWORD",
            os.environ.get("REDIS_PASSWORD", ""),
        ),
        redis_sentinel_service_name=os.environ.get(
            "NOMOS_SIGNUP_REDIS_SENTINEL_SERVICE_NAME",
            os.environ.get("NOMOS_REDIS_SENTINEL_SERVICE_NAME", ""),
        ),
        redis_sentinel_nodes=tuple(
            parse_csv(
                os.environ.get(
                    "NOMOS_SIGNUP_REDIS_SENTINEL_NODES",
                    os.environ.get("NOMOS_REDIS_SENTINEL_NODES", ""),
                )
            )
        ),
        redis_sentinel_password=os.environ.get(
            "NOMOS_SIGNUP_REDIS_SENTINEL_PASSWORD",
            os.environ.get("NOMOS_REDIS_SENTINEL_PASSWORD", ""),
        ),
        redis_connect_timeout_ms=int(
            os.environ.get(
                "NOMOS_SIGNUP_REDIS_CONNECT_TIMEOUT_MS",
                os.environ.get("NOMOS_REDIS_CONNECT_TIMEOUT_MS", "1000"),
            )
        ),
        redis_command_timeout_ms=int(
            os.environ.get(
                "NOMOS_SIGNUP_REDIS_COMMAND_TIMEOUT_MS",
                os.environ.get("NOMOS_REDIS_COMMAND_TIMEOUT_MS", "1000"),
            )
        ),
        default_council_id=default_council_id,
        allowed_councils=allowed_councils,
        default_role=default_role,
        allowed_roles=allowed_roles,
        council_endpoint=os.environ.get(
            "NOMOS_SIGNUP_COUNCIL_ENDPOINT",
            "http://localhost:9901/mcp",
        ),
        admin_user=os.environ.get("NOMOS_SIGNUP_ADMIN_USER", "admin"),
        admin_password=os.environ.get("NOMOS_SIGNUP_ADMIN_PASSWORD")
        or os.environ.get("MONITOR_DASHBOARD_PASSWORD")
        or "nomos-dev-admin",
        council_admin_user=os.environ.get("NOMOS_SIGNUP_COUNCIL_ADMIN_USER", "council-admin"),
        council_admin_password=os.environ.get("NOMOS_SIGNUP_COUNCIL_ADMIN_PASSWORD", ""),
        storage_mode=os.environ.get("COUNCIL_STORAGE_MODE", "redis").strip().lower(),
        database_url=os.environ.get("COUNCIL_DATABASE_URL") or os.environ.get("DATABASE_URL") or "",
        account_integrity_key=os.environ.get("NOMOS_ACCOUNT_INTEGRITY_KEY", ""),
    )
    validate_settings(settings)
    return settings


class ConsoleAuthMiddleware(BaseHTTPMiddleware):
    """Password-protect the whole console with Basic auth.

    Two credential sets: the signup console (everything) and a separate, more
    privileged credential for the council-creation page (``/council`` +
    ``/api/council``). Probes stay public. Mirrors demo.server.main's
    DemoAuthMiddleware (timing-safe compare, fail-closed when a password is unset).
    """

    _PUBLIC_PATHS: ClassVar[set[str]] = {"/healthz"}

    def __init__(self, app: Any, settings: SignupSettings):
        super().__init__(app)
        self._settings = settings

    @staticmethod
    def _credentials(request: Request) -> tuple[str, str] | None:
        auth = request.headers.get("Authorization", "")
        scheme, _, encoded = auth.partition(" ")
        if scheme.lower() != "basic" or not encoded:
            return None
        try:
            decoded = b64decode(encoded, validate=True).decode("utf-8")
        except Exception:
            return None
        user, sep, password = decoded.partition(":")
        if not sep:
            return None
        return user, password

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in self._PUBLIC_PATHS:
            return await call_next(request)

        is_council = path == "/council" or path.startswith("/api/council")
        if is_council:
            realm = "Nomos Council Admin"
            exp_user = self._settings.council_admin_user
            exp_password = self._settings.council_admin_password
        else:
            realm = "Nomos Agent Signup"
            exp_user = self._settings.admin_user
            exp_password = self._settings.admin_password

        if not exp_password:
            return PlainTextResponse("This page's password is not configured.", status_code=503)

        challenge = {"WWW-Authenticate": f'Basic realm="{realm}", charset="UTF-8"'}
        credentials = self._credentials(request)
        if credentials is None:
            return PlainTextResponse("Authentication required.", status_code=401, headers=challenge)
        user, password = credentials
        user_ok = secrets.compare_digest(user, exp_user)
        password_ok = secrets.compare_digest(password, exp_password)
        if not (user_ok and password_ok):
            return PlainTextResponse("Authentication required.", status_code=401, headers=challenge)
        return await call_next(request)


def create_app(
    settings: SignupSettings | None = None,
    *,
    account_store: Any = None,
) -> FastAPI:
    app_settings = settings or settings_from_env()
    validate_settings(app_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # In service (postgres) mode the console writes to the durable store the
        # council reads; pool it for the app lifetime. Tests inject account_store
        # directly and skip this.
        if (
            getattr(app.state, "account_store", None) is None
            and app_settings.storage_mode == "postgres"
        ):
            from nomos.store.postgres_store import PostgresStore

            store = PostgresStore(
                app_settings.database_url,
                account_integrity_key=app_settings.account_integrity_key,
            )
            await store.connect()
            app.state.account_store = store
            app.state.owns_store = True
        try:
            yield
        finally:
            if getattr(app.state, "owns_store", False):
                await app.state.account_store.close()

    app = FastAPI(title="Nomos Agent Signup", version="0.1.0", lifespan=lifespan)
    app.state.account_store = account_store
    app.state.owns_store = False
    app.add_middleware(ConsoleAuthMiddleware, settings=app_settings)

    def _account_store() -> Any:
        store = getattr(app.state, "account_store", None)
        if store is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="account store is not ready",
            )
        return store

    def _require_redis_mode() -> None:
        # Account administration is redis/dev-mode only; in service mode accounts
        # live in PostgreSQL and are managed via the console signup flow.
        if app_settings.storage_mode != "redis":
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail="account administration is unavailable in service mode",
            )

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return _render_index(app_settings, await get_signup_config(app_settings))

    @app.get("/council", response_class=HTMLResponse)
    async def council_page() -> str:
        return _render_council(app_settings)

    @app.get("/healthz", response_model=HealthResponse)
    async def healthz(response: Response) -> HealthResponse:
        ok = True
        if app_settings.storage_mode == "postgres":
            store = getattr(app.state, "account_store", None)
            ok = bool(store) and await store.health_check()
        else:
            try:
                client = redis_client(app_settings)
                try:
                    await client.ping()
                finally:
                    await client.aclose()
            except Exception:
                ok = False
        if not ok:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return HealthResponse(ok=False, redis="unreachable")
        return HealthResponse(ok=True, redis="ok")

    @app.post(
        "/api/agents",
        response_model=SignupResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_agent(request: SignupRequest) -> SignupResponse:
        if app_settings.storage_mode == "postgres":
            return await create_agent_account_pg(_account_store(), app_settings, request)
        return await create_agent_account(app_settings, request)

    @app.post("/api/skill")
    async def generate_skill(request: SkillRequest) -> Response:
        body = build_skill_md(app_settings, request)
        return Response(
            content=body,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="SKILL.md"'},
        )

    @app.post("/api/council", status_code=status.HTTP_501_NOT_IMPLEMENTED)
    async def create_council() -> Response:
        # STUB. Future: call the council MCP tool `council_create_council` with a
        # COUNCIL_MANAGEMENT_TOKEN. Dynamic council mutations are disabled in
        # postgres mode at the council, so wiring must go through the council's
        # MCP management surface — never a local PG write.
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="council creation is not yet wired; coming soon",
        )

    # ── account administration (redis / dev mode only) ────────────────
    @app.get("/admin", response_class=HTMLResponse)
    async def admin() -> str:
        _require_redis_mode()
        return _render_admin()

    @app.get("/api/admin/accounts", response_model=AccountListResponse)
    async def list_admin_accounts() -> AccountListResponse:
        _require_redis_mode()
        return AccountListResponse(accounts=await list_agent_accounts(app_settings))

    @app.get("/api/admin/config", response_model=SignupConfig)
    async def get_admin_config() -> SignupConfig:
        _require_redis_mode()
        return await get_signup_config(app_settings)

    @app.patch("/api/admin/config", response_model=SignupConfig)
    async def patch_admin_config(request: SignupConfigUpdate) -> SignupConfig:
        _require_redis_mode()
        return await update_signup_config(app_settings, request.to_config())

    @app.get("/api/admin/accounts/{agent_id}", response_model=AgentAccountRecord)
    async def get_admin_account(agent_id: str) -> AgentAccountRecord:
        _require_redis_mode()
        account = await get_agent_account(app_settings, agent_id)
        if account is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="agent not found")
        return account

    @app.patch("/api/admin/accounts/{agent_id}", response_model=AgentAccountRecord)
    async def patch_admin_account(
        agent_id: str,
        request: AccountUpdateRequest,
    ) -> AgentAccountRecord:
        _require_redis_mode()
        return await update_agent_account(app_settings, agent_id, request)

    @app.post(
        "/api/admin/accounts/{agent_id}/rotate-token",
        response_model=TokenRotateResponse,
    )
    async def rotate_admin_token(agent_id: str) -> TokenRotateResponse:
        _require_redis_mode()
        return await rotate_agent_token(app_settings, agent_id)

    @app.delete("/api/admin/accounts/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_admin_account(agent_id: str) -> Response:
        _require_redis_mode()
        await delete_agent_account(app_settings, agent_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return app


def redis_client(settings: SignupSettings) -> aioredis.Redis:
    tls = ClientTLSConfig.from_env("NOMOS_SIGNUP_REDIS")
    if strict_transport_profile(os.environ.get("NOMOS_DEPLOY_PROFILE")) and (
        not tls.mutual or not tls.verify_hostname
    ):
        raise ValueError(
            "agent-signup Redis mTLS with hostname verification is required "
            "in this deployment profile"
        )
    return create_redis_client(
        RedisConnectionConfig(
            mode=settings.redis_mode,
            url=settings.redis_url,
            password=settings.redis_password,
            sentinel_service_name=settings.redis_sentinel_service_name,
            sentinel_nodes=settings.redis_sentinel_nodes,
            sentinel_password=settings.redis_sentinel_password,
            connect_timeout_ms=settings.redis_connect_timeout_ms,
            command_timeout_ms=settings.redis_command_timeout_ms,
            tls=tls.enabled,
            tls_ca_file=tls.ca_file,
            tls_cert_file=tls.cert_file,
            tls_key_file=tls.key_file,
            tls_verify_hostname=tls.verify_hostname,
            tls_minimum_version=tls.minimum_version,
        )
    )


def parse_csv(raw: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def parse_allowed_roles(raw_roles: str) -> tuple[str, ...]:
    roles = tuple(dict.fromkeys(role.strip() for role in raw_roles.split(",") if role.strip()))
    if not roles:
        return ("developer",)
    invalid = sorted(set(roles) - set(ROLE_PERMISSIONS))
    if invalid:
        raise ValueError(
            "NOMOS_SIGNUP_ALLOWED_ROLES contains unknown role(s): " + ", ".join(invalid)
        )
    return roles


def parse_allowed_councils(raw_councils: str) -> tuple[str, ...]:
    councils = tuple(
        dict.fromkeys(
            council_id.strip() for council_id in raw_councils.split(",") if council_id.strip()
        )
    )
    if not councils:
        return ("council-prod",)
    invalid = [council_id for council_id in councils if not COUNCIL_ID_RE.fullmatch(council_id)]
    if invalid:
        raise ValueError(
            "NOMOS_SIGNUP_ALLOWED_COUNCILS contains invalid council id(s): " + ", ".join(invalid)
        )
    return councils


def validate_settings(settings: SignupSettings) -> None:
    invalid = sorted(set(settings.allowed_roles) - set(ROLE_PERMISSIONS))
    if invalid:
        raise ValueError("allowed_roles contains unknown role(s): " + ", ".join(invalid))
    if settings.default_role not in settings.allowed_roles:
        raise ValueError(
            f"default_role {settings.default_role!r} must be included in allowed_roles"
        )
    if not settings.allowed_councils:
        raise ValueError("allowed_councils must include at least one council id")
    invalid_councils = [
        council_id
        for council_id in settings.allowed_councils
        if not COUNCIL_ID_RE.fullmatch(council_id)
    ]
    if invalid_councils:
        raise ValueError(
            "allowed_councils contains invalid council id(s): " + ", ".join(invalid_councils)
        )
    if settings.default_council_id not in settings.allowed_councils:
        raise ValueError(
            f"default_council_id {settings.default_council_id!r} "
            "must be included in allowed_councils"
        )
    if not settings.admin_user:
        raise ValueError("admin_user must not be empty")
    if not settings.admin_password:
        raise ValueError("admin_password must not be empty")
    if settings.storage_mode not in {"redis", "postgres"}:
        raise ValueError("storage_mode must be redis or postgres")
    if settings.storage_mode == "postgres":
        if not settings.database_url:
            raise ValueError("COUNCIL_DATABASE_URL is required when COUNCIL_STORAGE_MODE=postgres")
        if not settings.account_integrity_key:
            raise ValueError(
                "NOMOS_ACCOUNT_INTEGRITY_KEY is required in service (postgres) mode; "
                "the council rejects accounts whose integrity HMAC is unset"
            )
    validate_deployment_preflight(
        profile=os.environ.get("NOMOS_DEPLOY_PROFILE"),
        secrets=[
            SecretCheck("NOMOS_SIGNUP_ADMIN_PASSWORD", settings.admin_password),
            SecretCheck("NOMOS_ACCOUNT_INTEGRITY_KEY", settings.account_integrity_key),
        ],
    )
    if settings.redis_mode not in {"standalone", "sentinel"}:
        raise ValueError("redis_mode must be standalone or sentinel")
    if settings.redis_mode == "sentinel":
        if not settings.redis_sentinel_service_name:
            raise ValueError("redis_sentinel_service_name is required in sentinel mode")
        if not settings.redis_sentinel_nodes:
            raise ValueError("redis_sentinel_nodes is required in sentinel mode")


async def create_agent_account(
    settings: SignupSettings,
    request: SignupRequest,
) -> SignupResponse:
    signup_config = await get_signup_config(settings)
    council_id = request.council_id or signup_config.default_council_id
    if council_id not in signup_config.allowed_councils:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"council_id {council_id!r} is not allowed by this signup service",
        )
    role = request.role or settings.default_role
    if role not in settings.allowed_roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"role {role!r} is not allowed by this signup service",
        )
    permissions = permissions_for_role(role)
    display_name = request.display_name or request.agent_id
    raw_token = secrets.token_hex(32)
    hashed_token = hashlib.sha256(raw_token.encode()).hexdigest()
    now = str(time.time())
    te_fields: dict[str, str] = {}

    client = redis_client(settings)
    try:
        await client.ping()
        key = account_key(settings.redis_prefix, request.agent_id)
        if await client.exists(key):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="agent_id already exists; choose a different id",
            )

        pipe = client.pipeline()
        pipe.hset(
            key,
            mapping={
                "token_hash": hashed_token,
                "display_name": display_name,
                "created_at": now,
                "updated_at": now,
                "enabled": "1",
                "role": role,
            },
        )
        if permissions:
            pipe.sadd(perms_key(settings.redis_prefix, request.agent_id), *permissions)
        pipe.sadd(account_councils_key(settings.redis_prefix, request.agent_id), council_id)
        pipe.sadd(council_agents_key(settings.redis_prefix, council_id), request.agent_id)
        pipe.set(token_index_key(settings.redis_prefix, hashed_token), request.agent_id)
        await pipe.execute()
        await refresh_account_integrity(client, settings, request.agent_id)
        if request.has_trusted_executor:
            # The account and its token index are already committed above, so the
            # account authenticates from here on while its raw token exists only
            # in the response we would be abandoning. Roll back before failing.
            try:
                te_fields = await _resolve_and_register_te(
                    _RedisTrustedExecutors(client, settings.redis_prefix), request
                )
            except Exception as te_exc:
                await _rollback_redis_account(
                    client, settings, request.agent_id, hashed_token, council_id
                )
                if isinstance(te_exc, HTTPException):
                    raise
                _log_signup_failure(
                    "trusted-executor registration failed", request.agent_id, te_exc
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="could not register trusted executor",
                ) from te_exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="could not create agent account",
        ) from exc
    finally:
        await client.aclose()

    return SignupResponse(
        agent_id=request.agent_id,
        display_name=display_name,
        council_endpoint=settings.council_endpoint,
        council_id=council_id,
        role=role,
        permissions=list(permissions),
        auth_token=raw_token,
        **te_fields,
    )


async def list_agent_accounts(settings: SignupSettings) -> list[AgentAccountRecord]:
    client = redis_client(settings)
    try:
        await client.ping()
        account_prefix = f"{settings.redis_prefix}accounts:"
        accounts: list[AgentAccountRecord] = []
        async for key in client.scan_iter(match=f"{account_prefix}*"):
            agent_id = key.removeprefix(account_prefix)
            if ":" in agent_id:
                continue
            account = await load_agent_account(client, settings, agent_id)
            if account is not None:
                accounts.append(account)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="could not list agent accounts",
        ) from exc
    finally:
        await client.aclose()
    return sorted(accounts, key=lambda account: account.agent_id)


async def get_agent_account(
    settings: SignupSettings,
    agent_id: str,
) -> AgentAccountRecord | None:
    validate_agent_id(agent_id)
    client = redis_client(settings)
    try:
        await client.ping()
        return await load_agent_account(client, settings, agent_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="could not get agent account",
        ) from exc
    finally:
        await client.aclose()


async def update_agent_account(
    settings: SignupSettings,
    agent_id: str,
    request: AccountUpdateRequest,
) -> AgentAccountRecord:
    validate_agent_id(agent_id)
    client = redis_client(settings)
    try:
        await client.ping()
        key = account_key(settings.redis_prefix, agent_id)
        existing = await client.hgetall(key)
        if not existing:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="agent not found")

        now = str(time.time())
        mapping: dict[str, str] = {"updated_at": now}
        if request.display_name is not None:
            mapping["display_name"] = request.display_name or agent_id
        if request.enabled is not None:
            mapping["enabled"] = "1" if request.enabled else "0"
        if request.role is not None:
            mapping["role"] = request.role

        pipe = client.pipeline()
        pipe.hset(key, mapping=mapping)
        if request.role is not None:
            pipe.delete(perms_key(settings.redis_prefix, agent_id))
            permissions = permissions_for_role(request.role)
            if permissions:
                pipe.sadd(perms_key(settings.redis_prefix, agent_id), *permissions)
        if request.councils is not None:
            signup_config = await get_signup_config(settings)
            new_councils = set(dict.fromkeys(request.councils))
            disallowed = sorted(new_councils - set(signup_config.allowed_councils))
            if disallowed:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="council_id is not allowed by this signup service: "
                    + ", ".join(disallowed),
                )
            councils_key = account_councils_key(settings.redis_prefix, agent_id)
            old_councils = set(await client.smembers(councils_key))
            pipe.delete(councils_key)
            for council_id in old_councils - new_councils:
                pipe.srem(council_agents_key(settings.redis_prefix, council_id), agent_id)
            for council_id in new_councils:
                pipe.sadd(councils_key, council_id)
                pipe.sadd(council_agents_key(settings.redis_prefix, council_id), agent_id)
        await pipe.execute()
        await refresh_account_integrity(client, settings, agent_id)

        account = await load_agent_account(client, settings, agent_id)
        if account is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="agent not found")
        return account
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="could not update agent account",
        ) from exc
    finally:
        await client.aclose()


async def rotate_agent_token(settings: SignupSettings, agent_id: str) -> TokenRotateResponse:
    validate_agent_id(agent_id)
    raw_token = secrets.token_hex(32)
    hashed_token = hashlib.sha256(raw_token.encode()).hexdigest()
    client = redis_client(settings)
    try:
        await client.ping()
        key = account_key(settings.redis_prefix, agent_id)
        existing = await client.hgetall(key)
        if not existing:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="agent not found")

        old_hash = existing.get("token_hash")
        pipe = client.pipeline()
        pipe.hset(
            key,
            mapping={
                "token_hash": hashed_token,
                "updated_at": str(time.time()),
            },
        )
        pipe.hdel(key, "auth_token")
        if old_hash:
            pipe.delete(token_index_key(settings.redis_prefix, old_hash))
        pipe.set(token_index_key(settings.redis_prefix, hashed_token), agent_id)
        await pipe.execute()
        await refresh_account_integrity(client, settings, agent_id)

        account = await load_agent_account(client, settings, agent_id)
        if account is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="agent not found")
        return TokenRotateResponse(account=account, auth_token=raw_token)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="could not rotate agent token",
        ) from exc
    finally:
        await client.aclose()


async def delete_agent_account(settings: SignupSettings, agent_id: str) -> None:
    validate_agent_id(agent_id)
    client = redis_client(settings)
    try:
        await client.ping()
        key = account_key(settings.redis_prefix, agent_id)
        existing = await client.hgetall(key)
        if not existing:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="agent not found")

        token_hash = existing.get("token_hash")
        councils = set(await client.smembers(account_councils_key(settings.redis_prefix, agent_id)))
        pipe = client.pipeline()
        pipe.delete(key)
        pipe.delete(perms_key(settings.redis_prefix, agent_id))
        pipe.delete(account_councils_key(settings.redis_prefix, agent_id))
        for council_id in councils:
            pipe.srem(council_agents_key(settings.redis_prefix, council_id), agent_id)
        if token_hash:
            pipe.delete(token_index_key(settings.redis_prefix, token_hash))
        await pipe.execute()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="could not delete agent account",
        ) from exc
    finally:
        await client.aclose()


async def get_signup_config(settings: SignupSettings) -> SignupConfig:
    fallback = SignupConfig(
        default_council_id=settings.default_council_id,
        allowed_councils=list(settings.allowed_councils),
    )
    if settings.storage_mode == "postgres":
        # No redis-backed signup config in service mode; use the static allow-list.
        return fallback
    client = redis_client(settings)
    try:
        await client.ping()
        data = await client.hgetall(signup_config_key(settings.redis_prefix))
    except Exception:
        return fallback
    finally:
        await client.aclose()

    if not data:
        return fallback
    allowed_councils = parse_allowed_councils(
        data.get("allowed_councils", ",".join(fallback.allowed_councils))
    )
    default_council_id = data.get("default_council_id") or fallback.default_council_id
    if default_council_id not in allowed_councils:
        default_council_id = allowed_councils[0]
    return SignupConfig(
        default_council_id=default_council_id,
        allowed_councils=list(allowed_councils),
    )


async def update_signup_config(settings: SignupSettings, config: SignupConfig) -> SignupConfig:
    client = redis_client(settings)
    try:
        await client.ping()
        await client.hset(
            signup_config_key(settings.redis_prefix),
            mapping={
                "default_council_id": config.default_council_id,
                "allowed_councils": ",".join(config.allowed_councils),
                "updated_at": str(time.time()),
            },
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="could not update signup config",
        ) from exc
    finally:
        await client.aclose()
    return config


async def load_agent_account(
    client: aioredis.Redis,
    settings: SignupSettings,
    agent_id: str,
) -> AgentAccountRecord | None:
    data = await client.hgetall(account_key(settings.redis_prefix, agent_id))
    if not data:
        return None
    if "auth_token" in data:
        await client.hdel(account_key(settings.redis_prefix, agent_id), "auth_token")
        data.pop("auth_token", None)
    permissions = sorted(await client.smembers(perms_key(settings.redis_prefix, agent_id)))
    councils = sorted(await client.smembers(account_councils_key(settings.redis_prefix, agent_id)))
    token_hash = data.get("token_hash", "")
    return AgentAccountRecord(
        agent_id=agent_id,
        display_name=data.get("display_name") or agent_id,
        enabled=data.get("enabled", "1") == "1",
        role=data.get("role") or "unknown",
        permissions=permissions,
        councils=councils,
        council_endpoint=settings.council_endpoint,
        created_at=data.get("created_at", ""),
        updated_at=data.get("updated_at", ""),
        token_hash_prefix=token_hash[:12],
    )


async def refresh_account_integrity(
    client: aioredis.Redis,
    settings: SignupSettings,
    agent_id: str,
) -> None:
    if not settings.account_integrity_key:
        return
    data = await client.hgetall(account_key(settings.redis_prefix, agent_id))
    if not data:
        return
    permissions = set(await client.smembers(perms_key(settings.redis_prefix, agent_id)))
    councils = set(await client.smembers(account_councils_key(settings.redis_prefix, agent_id)))
    signature = sign_account_record(
        key=settings.account_integrity_key,
        agent_id=agent_id,
        account=data,
        permissions=permissions,
        councils=councils,
    )
    await client.hset(
        account_key(settings.redis_prefix, agent_id),
        mapping={ACCOUNT_INTEGRITY_FIELD: signature},
    )


def validate_agent_id(agent_id: str) -> None:
    if not AGENT_ID_RE.fullmatch(agent_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="agent_id must contain only letters, numbers, hyphen, or underscore",
        )


def permissions_for_role(role: str) -> tuple[str, ...]:
    """Package role lookup, surfaced as this service's HTTP error."""
    try:
        return _pkg_permissions_for_role(role)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown role {role!r}",
        ) from exc


def account_key(prefix: str, agent_id: str) -> str:
    return f"{prefix}accounts:{agent_id}"


def perms_key(prefix: str, agent_id: str) -> str:
    return f"{prefix}accounts:{agent_id}:perms"


def account_councils_key(prefix: str, agent_id: str) -> str:
    return f"{prefix}accounts:{agent_id}:councils"


def council_agents_key(prefix: str, council_id: str) -> str:
    return f"{prefix}councils:{council_id}:agents"


def token_index_key(prefix: str, hashed_token: str) -> str:
    return f"{prefix}token_index:{hashed_token}"


def signup_config_key(prefix: str) -> str:
    return f"{prefix}signup:config"


def _render_index(settings: SignupSettings, signup_config: SignupConfig) -> str:
    escaped_endpoint = html.escape(settings.council_endpoint)
    council_options = "\n".join(
        f'<option value="{html.escape(council_id)}"'
        f"{' selected' if council_id == signup_config.default_council_id else ''}>"
        f"{html.escape(council_id)}</option>"
        for council_id in signup_config.allowed_councils
    )
    role_options = "\n".join(
        f'<option value="{html.escape(role)}"'
        f"{' selected' if role == settings.default_role else ''}>{html.escape(role)}</option>"
        for role in settings.allowed_roles
    )
    # Plain string with single braces; only the __PLACEHOLDER__ tokens are substituted.
    html_doc = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Nomos Agent Console</title>
  <style>
    :root {
      color-scheme: light;
      --bg-grad-a: #f6f8fc; --bg-grad-b: #e6ecf6;
      --panel: #ffffff; --panel-soft: #f7f9fc;
      --ink: #0e1726; --ink-soft: #2a3a55; --muted: #66748b;
      --line: #d6deeb; --line-soft: #e7ecf4;
      --accent: #1f4fd4; --accent-deep: #0d2b8a; --accent-soft: #e7eeff;
      --green: #0a7a52; --red: #c0392f; --amber: #8a5a00; --amber-bg: #fff7e6; --amber-line: #f3d28a;
      --code-bg: #0f172a; --code-ink: #e3ebff;
      --shadow-md: 0 10px 30px rgba(15, 30, 70, 0.08);
    }
    * { box-sizing: border-box; }
    html, body { margin: 0; }
    body {
      min-height: 100vh;
      font: 14.5px/1.55 Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(1100px 600px at 100% -10%, rgba(31, 79, 212, 0.10), transparent 60%),
        linear-gradient(180deg, var(--bg-grad-a), var(--bg-grad-b));
    }
    header {
      padding: 26px clamp(18px, 5vw, 64px) 18px;
      background: linear-gradient(135deg, #0c1530 0%, #16244b 55%, #1f4fd4 130%);
      color: #fff;
    }
    .brand { display: flex; align-items: center; gap: 10px; font-size: 12px;
      letter-spacing: 0.18em; text-transform: uppercase; color: rgba(255,255,255,0.65); }
    .brand .dot { width: 8px; height: 8px; border-radius: 50%; background: #4cc38a;
      box-shadow: 0 0 0 4px rgba(76,195,138,0.18); }
    h1 { margin: 8px 0 4px; font-size: clamp(26px, 4vw, 38px); font-weight: 700; }
    header p { margin: 0; color: rgba(255,255,255,0.78); max-width: 64ch; }
    .tabs { display: flex; gap: 8px; padding: 0 clamp(18px, 5vw, 64px);
      background: linear-gradient(135deg, #16244b, #1f4fd4 220%); }
    .tab { appearance: none; border: 0; background: transparent; color: rgba(255,255,255,0.7);
      font: inherit; font-weight: 600; padding: 12px 16px; cursor: pointer;
      border-bottom: 3px solid transparent; }
    .tab[aria-selected="true"] { color: #fff; border-bottom-color: #4cc38a; }
    .tab-link { margin-left: auto; align-self: center; color: rgba(255,255,255,0.85);
      font-size: 13px; text-decoration: none; padding-right: 2px; }
    .tab-link:hover { text-decoration: underline; }
    main { padding: 24px clamp(18px, 5vw, 64px) 56px; }
    .grid { display: grid; grid-template-columns: minmax(280px, 380px) minmax(320px, 1fr);
      gap: 22px; align-items: start; }
    section { background: var(--panel); border: 1px solid var(--line); border-radius: 14px;
      box-shadow: var(--shadow-md); overflow: hidden; }
    .panel-head { padding: 18px 20px 12px; border-bottom: 1px solid var(--line-soft); }
    .panel-head h2 { margin: 0; font-size: 17px; font-weight: 700; }
    .panel-head .subtle { margin: 4px 0 0; color: var(--muted); font-size: 13px; }
    .panel-body { padding: 18px 20px 20px; }
    label { display: block; margin: 14px 0 6px; font-weight: 600; font-size: 13px; color: var(--ink-soft); }
    label:first-of-type { margin-top: 4px; }
    input, select, textarea { width: 100%; min-height: 42px; border: 1px solid var(--line);
      border-radius: 8px; padding: 9px 12px; font: inherit; color: var(--ink); background: #fff; }
    input:focus, select:focus, textarea:focus { outline: none; border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(31,79,212,0.18); }
    textarea { resize: vertical; min-height: 64px; font-family: ui-monospace, "JetBrains Mono", Menlo, monospace; font-size: 12.5px; }
    fieldset.te { margin-top: 16px; border: 1px solid var(--line); border-radius: 10px; padding: 10px 14px 14px; }
    fieldset.te legend { font-size: 12px; font-weight: 700; color: var(--ink-soft); padding: 0 6px; }
    fieldset.te .choice { display: flex; align-items: center; gap: 8px; margin: 8px 0; font-weight: 500; }
    fieldset.te .choice input { width: auto; min-height: 0; }
    button { margin-top: 18px; min-height: 44px; width: 100%; border: 0; border-radius: 9px;
      padding: 0 16px; background: linear-gradient(180deg, var(--accent), var(--accent-deep));
      color: #fff; font: inherit; font-weight: 700; cursor: pointer;
      box-shadow: 0 4px 14px rgba(31,79,212,0.28); transition: box-shadow .15s, opacity .15s; }
    button:hover:not(:disabled) { box-shadow: 0 6px 18px rgba(31,79,212,0.36); }
    button:disabled { opacity: .55; cursor: progress; box-shadow: none; }
    .btn-secondary { background: #fff; color: var(--accent-deep); border: 1px solid var(--accent);
      box-shadow: none; }
    dl { margin: 0; display: grid; grid-template-columns: 120px minmax(0, 1fr); row-gap: 10px; column-gap: 14px; }
    dt { color: var(--muted); font-weight: 600; font-size: 12px; text-transform: uppercase;
      letter-spacing: 0.05em; align-self: center; }
    dd { margin: 0; word-break: break-all; font-weight: 500; }
    .token-field { display: flex; align-items: stretch; gap: 8px; }
    .token-field code { flex: 1; display: block; padding: 9px 12px; border-radius: 8px;
      border: 1px solid var(--line); background: var(--panel-soft);
      font-family: ui-monospace, "JetBrains Mono", Menlo, monospace; font-size: 12.5px; overflow-wrap: anywhere; }
    .copy-btn { margin: 0; width: auto; min-height: 38px; padding: 0 12px; background: #fff;
      color: var(--ink-soft); border: 1px solid var(--line); border-radius: 8px; box-shadow: none;
      font-size: 12px; font-weight: 600; cursor: pointer; }
    .copy-btn:hover { background: var(--accent-soft); color: var(--accent-deep); border-color: var(--accent); }
    .copy-btn.copied { background: var(--green); color: #fff; border-color: var(--green); }
    .empty { color: var(--muted); padding: 28px 8px; text-align: center; border: 1px dashed var(--line);
      border-radius: 10px; background: var(--panel-soft); }
    .empty .hint { display: block; margin-top: 6px; font-size: 12.5px; }
    .warn { margin-top: 14px; padding: 11px 13px; border-radius: 9px; background: var(--amber-bg);
      border: 1px solid var(--amber-line); color: var(--amber); font-size: 12.5px; }
    .status { margin-top: 14px; min-height: 22px; font-weight: 600; font-size: 13px; }
    .status.ok { color: var(--green); }
    .status.error { color: var(--red); }
    .quickstart { margin-top: 20px; display: grid; gap: 22px; }
    .client-section { border-top: 1px solid var(--line-soft); padding-top: 18px; }
    .client-head { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
    .client-badge { display: inline-flex; padding: 4px 12px; background: linear-gradient(135deg, var(--accent), var(--accent-deep));
      color: #fff; border-radius: 999px; font-size: 12px; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; }
    .client-badge.codex { background: linear-gradient(135deg, #1d8a6b, #0a5a44); }
    .client-sub { color: var(--muted); font-size: 13px; }
    .steps { display: grid; gap: 12px; }
    .step { border: 1px solid var(--line); border-radius: 12px; overflow: hidden; }
    .step-head { display: flex; align-items: center; gap: 10px; padding: 9px 14px;
      border-bottom: 1px solid var(--line-soft); background: var(--panel-soft); }
    .step-title { font-weight: 600; font-size: 13.5px; color: var(--ink-soft); }
    .step-head .copy-btn { margin-left: auto; }
    pre { margin: 0; padding: 14px 16px; background: var(--code-bg); color: var(--code-ink);
      font-family: ui-monospace, "JetBrains Mono", Menlo, monospace; font-size: 12.5px;
      line-height: 1.55; overflow-x: auto; white-space: pre-wrap; word-break: break-word; }
    .hidden, [hidden] { display: none !important; }
    .help { color: var(--muted); font-size: 12px; margin: 4px 0 0; }
    @media (max-width: 880px) {
      .grid { grid-template-columns: 1fr; }
      dl { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <div class="brand"><span class="dot" aria-hidden="true"></span>Nomos Security Council</div>
    <h1>Agent Console</h1>
    <p>Register an agent, attach a trusted executor, and generate the SKILL.md that teaches
    your agent to route privileged actions through the council.</p>
  </header>
  <div class="tabs" role="tablist">
    <button class="tab" role="tab" id="tab-signup" aria-selected="true" data-tab="signup">Sign up an agent</button>
    <button class="tab" role="tab" id="tab-skill" aria-selected="false" data-tab="skill">Generate SKILL.md</button>
    <a class="tab-link" href="/council">Create a council &rarr;</a>
  </div>
  <main>
    <div class="tabpanel" data-panel="signup">
      <div class="grid">
        <section>
          <div class="panel-head">
            <h2>Create Agent</h2>
            <p class="subtle">Issues an agent ID, role-scoped permissions, and a one-time auth token.</p>
          </div>
          <div class="panel-body">
            <form id="signup-form">
              <label for="agent-id">Agent ID</label>
              <input id="agent-id" name="agent_id" autocomplete="off" required
                pattern="[A-Za-z0-9_-]+" maxlength="80" placeholder="alice-agent">

              <label for="display-name">Display Name</label>
              <input id="display-name" name="display_name" autocomplete="name" maxlength="120" placeholder="Alice">

              <label for="role">Role</label>
              <select id="role" name="role">__ROLE_OPTIONS__</select>

              <label for="council-id">Council</label>
              <select id="council-id" name="council_id">__COUNCIL_OPTIONS__</select>

              <fieldset class="te">
                <legend>Trusted executor</legend>
                <label class="choice"><input type="radio" name="te_choice" value="none" checked> No trusted executor</label>
                <label class="choice"><input type="radio" name="te_choice" value="paste"> I'll provide the executor public key</label>
                <label class="choice"><input type="radio" name="te_choice" value="generate"> Generate a keypair for me</label>
                <div id="te-paste" hidden>
                  <label for="te-pubkey">Executor public key</label>
                  <input id="te-pubkey" name="te_public_key_hex" autocomplete="off"
                    placeholder="64-char hex (ed25519)">
                </div>
                <div id="te-exec-wrap" hidden>
                  <label for="te-exec-id">Executor ID (optional)</label>
                  <input id="te-exec-id" name="te_executor_id" autocomplete="off"
                    placeholder="agent:&lt;fp16&gt; (auto)">
                  <p class="help">Derived from the key fingerprint if left blank.</p>
                </div>
              </fieldset>

              <button id="submit" type="submit">Create Agent</button>
              <div id="status" class="status" aria-live="polite"></div>
            </form>
          </div>
        </section>
        <section>
          <div class="panel-head">
            <h2>Credentials &amp; Client Setup</h2>
            <p class="subtle">Copy these once &mdash; the token and any private key are shown one time.</p>
          </div>
          <div class="panel-body">
            <div id="empty" class="empty">
              No agent created yet.
              <span class="hint">Submit the form to mint an account and (optionally) register a trusted executor.</span>
            </div>
            <dl id="result" hidden>
              <dt>Agent&nbsp;ID</dt><dd id="out-agent"></dd>
              <dt>Endpoint</dt><dd id="out-endpoint">__ESCAPED_ENDPOINT__</dd>
              <dt>Token</dt>
              <dd>
                <div class="token-field">
                  <code id="out-token"></code>
                  <button type="button" class="copy-btn" data-copy-target="out-token">Copy</button>
                </div>
              </dd>
              <dt>Role</dt><dd id="out-role"></dd>
              <dt>Council</dt><dd id="out-council"></dd>
              <div id="te-rows" style="display: contents;" hidden>
                <dt>Executor&nbsp;ID</dt><dd id="out-te-id"></dd>
                <dt>Key&nbsp;FP</dt><dd id="out-te-fp"></dd>
                <dt>TE&nbsp;Pubkey</dt><dd id="out-te-pub"></dd>
                <dt id="te-priv-dt" hidden>TE&nbsp;Private</dt>
                <dd id="te-priv-dd" hidden>
                  <div class="token-field">
                    <code id="out-te-priv"></code>
                    <button type="button" class="copy-btn" data-copy-target="out-te-priv">Copy</button>
                  </div>
                </dd>
              </div>
            </dl>
            <div id="te-warn" class="warn" hidden>
              The trusted-executor <strong>private key</strong> is shown only once and is never stored.
              Save it now and configure it on your executor.
            </div>
            <button id="download-creds" class="btn-secondary" type="button" hidden>Download credentials (.json)</button>
            <div id="quickstart" class="quickstart" hidden>
              <div class="client-section">
                <div class="client-head">
                  <span class="client-badge">Claude Code</span>
                  <span class="client-sub">PreToolUse hook for Bash</span>
                </div>
                <div class="steps">
                  <div class="step">
                    <div class="step-head">
                      <span class="step-title">Install &amp; configure council-gate</span>
                      <button type="button" class="copy-btn" data-copy-target="claude-install">Copy</button>
                    </div>
                    <pre id="claude-install"></pre>
                  </div>
                  <div class="step">
                    <div class="step-head">
                      <span class="step-title">Enable in Claude Code</span>
                      <button type="button" class="copy-btn" data-copy-target="claude-enable">Copy</button>
                    </div>
                    <pre id="claude-enable"></pre>
                  </div>
                </div>
              </div>
              <div class="client-section">
                <div class="client-head">
                  <span class="client-badge codex">Codex</span>
                  <span class="client-sub">PreToolUse hook for shell_command</span>
                </div>
                <div class="steps">
                  <div class="step">
                    <div class="step-head">
                      <span class="step-title">Write Codex council-gate config</span>
                      <button type="button" class="copy-btn" data-copy-target="codex-config">Copy</button>
                    </div>
                    <pre id="codex-config"></pre>
                  </div>
                  <div class="step">
                    <div class="step-head">
                      <span class="step-title">Install &amp; enable</span>
                      <button type="button" class="copy-btn" data-copy-target="codex-install">Copy</button>
                    </div>
                    <pre id="codex-install"></pre>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </section>
      </div>
    </div>

    <div class="tabpanel" data-panel="skill" hidden>
      <div class="grid">
        <section>
          <div class="panel-head">
            <h2>Generate SKILL.md</h2>
            <p class="subtle">A drop-in skill file that teaches your agent the council protocol.</p>
          </div>
          <div class="panel-body">
            <form id="skill-form">
              <label for="skill-agent-id">Agent ID</label>
              <input id="skill-agent-id" name="agent_id" autocomplete="off" required
                pattern="[A-Za-z0-9_-]+" maxlength="80" placeholder="alice-agent">

              <label for="skill-council-id">Council</label>
              <select id="skill-council-id" name="council_id">__COUNCIL_OPTIONS__</select>

              <label for="skill-endpoint">Council endpoint</label>
              <input id="skill-endpoint" name="council_endpoint" autocomplete="off"
                value="__ESCAPED_ENDPOINT__">

              <label>Integration mode</label>
              <label class="choice"><input type="radio" name="mode" value="both" checked> Both (tool use + direct MCP)</label>
              <label class="choice"><input type="radio" name="mode" value="tool-use"> Tool use only</label>
              <label class="choice"><input type="radio" name="mode" value="mcp"> Direct MCP only</label>

              <label class="choice" style="margin-top:14px;">
                <input type="checkbox" id="skill-te" name="has_trusted_executor"> Agent has a trusted executor
              </label>

              <button id="skill-submit" type="submit">Generate &amp; download SKILL.md</button>
              <div id="skill-status" class="status" aria-live="polite"></div>
            </form>
          </div>
        </section>
        <section>
          <div class="panel-head">
            <h2>What you get</h2>
            <p class="subtle">A Markdown skill, ready to drop into your agent.</p>
          </div>
          <div class="panel-body">
            <div class="empty">
              SKILL.md describes how the agent submits intents, reads verdicts, and when to
              revise vs. accept a denial &mdash; in your chosen integration mode. Pair it with
              the credentials from the Sign up tab and point your agent at the council.
            </div>
          </div>
        </section>
      </div>
    </div>
  </main>
  <script>
    function shellQuote(value) {
      return "'" + String(value).replaceAll("'", "'\\\\''") + "'";
    }

    function renderClaude(data) {
      document.getElementById("claude-install").textContent = [
        "plugins/scripts/install.sh council-gate --non-interactive \\\\",
        `  --backend ${data.council_endpoint} \\\\`,
        `  --agent-id ${data.agent_id} \\\\`,
        `  --auth-token ${shellQuote(data.auth_token)} \\\\`,
        `  --council-id ${data.council_id} \\\\`,
        "  --timeout-ms 60000",
      ].join("\\n");
      document.getElementById("claude-enable").textContent = [
        "# In a Claude Code session:",
        "/council-gate on",
        "/council-gate status",
      ].join("\\n");
    }

    function renderCodex(data) {
      document.getElementById("codex-config").textContent = [
        "mkdir -p ~/.codex/plugin-state/council-gate",
        "chmod 700 ~/.codex/plugin-state/council-gate",
        "cat > ~/.codex/plugin-state/council-gate/.env <<'EOF'",
        `CODEX_COUNCIL_URL=${data.council_endpoint}`,
        `CODEX_COUNCIL_AGENT_ID=${data.agent_id}`,
        `CODEX_COUNCIL_AUTH_TOKEN=${data.auth_token}`,
        `CODEX_COUNCIL_ID=${data.council_id}`,
        "CODEX_COUNCIL_TIMEOUT_MS=60000",
        "EOF",
        "chmod 600 ~/.codex/plugin-state/council-gate/.env",
      ].join("\\n");
      document.getElementById("codex-install").textContent = [
        "plugins/codex-council-gate/scripts/install-local.sh",
        "plugins/codex-council-gate/scripts/toggle on",
      ].join("\\n");
    }

    function setupTabs() {
      const tabs = document.querySelectorAll(".tab");
      tabs.forEach((tab) => {
        tab.addEventListener("click", () => {
          tabs.forEach((t) => t.setAttribute("aria-selected", String(t === tab)));
          const want = tab.dataset.tab;
          document.querySelectorAll(".tabpanel").forEach((panel) => {
            panel.hidden = panel.dataset.panel !== want;
          });
        });
      });
    }

    function setupTeChoice() {
      const paste = document.getElementById("te-paste");
      const execWrap = document.getElementById("te-exec-wrap");
      document.querySelectorAll('input[name="te_choice"]').forEach((radio) => {
        radio.addEventListener("change", () => {
          const choice = document.querySelector('input[name="te_choice"]:checked').value;
          paste.hidden = choice !== "paste";
          execWrap.hidden = choice === "none";
        });
      });
    }

    function setupCopyButtons() {
      document.addEventListener("click", async (event) => {
        const button = event.target.closest(".copy-btn");
        if (!button) return;
        const target = document.getElementById(button.getAttribute("data-copy-target"));
        if (!target) return;
        try {
          await navigator.clipboard.writeText(target.textContent || "");
        } catch (_err) {
          const range = document.createRange();
          range.selectNodeContents(target);
          const sel = window.getSelection();
          sel.removeAllRanges();
          sel.addRange(range);
          document.execCommand("copy");
          sel.removeAllRanges();
        }
        const original = button.textContent;
        button.textContent = "Copied";
        button.classList.add("copied");
        setTimeout(() => { button.textContent = original; button.classList.remove("copied"); }, 1400);
      });
    }

    function showResult(data) {
      document.getElementById("out-agent").textContent = data.agent_id;
      document.getElementById("out-endpoint").textContent = data.council_endpoint;
      document.getElementById("out-token").textContent = data.auth_token;
      document.getElementById("out-role").textContent = data.role;
      document.getElementById("out-council").textContent = data.council_id;

      const teRows = document.getElementById("te-rows");
      if (data.te_executor_id) {
        document.getElementById("out-te-id").textContent = data.te_executor_id;
        document.getElementById("out-te-fp").textContent = data.te_key_fingerprint;
        document.getElementById("out-te-pub").textContent = data.te_public_key_hex;
        const hasPriv = Boolean(data.te_private_key_hex);
        document.getElementById("te-priv-dt").hidden = !hasPriv;
        document.getElementById("te-priv-dd").hidden = !hasPriv;
        document.getElementById("out-te-priv").textContent = data.te_private_key_hex || "";
        document.getElementById("te-warn").hidden = !hasPriv;
        teRows.hidden = false;
      } else {
        teRows.hidden = true;
        document.getElementById("te-warn").hidden = true;
      }

      renderClaude(data);
      renderCodex(data);
      document.getElementById("empty").hidden = true;
      document.getElementById("result").hidden = false;
      document.getElementById("quickstart").hidden = false;
      const dl = document.getElementById("download-creds");
      dl.hidden = false;
      dl.onclick = () => downloadCredentials(data);
    }

    function downloadCredentials(data) {
      const creds = {
        agent_id: data.agent_id,
        council_id: data.council_id,
        council_endpoint: data.council_endpoint,
        role: data.role,
        permissions: data.permissions,
        auth_token: data.auth_token,
      };
      if (data.te_executor_id) {
        creds.trusted_executor = {
          executor_id: data.te_executor_id,
          key_fingerprint: data.te_key_fingerprint,
          public_key_hex: data.te_public_key_hex,
        };
        if (data.te_private_key_hex) creds.trusted_executor.private_key_hex = data.te_private_key_hex;
      }
      const blob = new Blob([JSON.stringify(creds, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `nomos-${data.agent_id}-credentials.json`;
      a.click();
      URL.revokeObjectURL(url);
    }

    function wireSignup() {
      const form = document.getElementById("signup-form");
      const submit = document.getElementById("submit");
      const statusEl = document.getElementById("status");
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        submit.disabled = true;
        statusEl.className = "status";
        statusEl.textContent = "";
        const choice = document.querySelector('input[name="te_choice"]:checked').value;
        const payload = {
          agent_id: document.getElementById("agent-id").value.trim(),
          display_name: document.getElementById("display-name").value.trim(),
          role: document.getElementById("role").value,
          council_id: document.getElementById("council-id").value,
          has_trusted_executor: choice !== "none",
          te_mode: choice === "none" ? "" : choice,
          te_public_key_hex: document.getElementById("te-pubkey").value.trim(),
          te_executor_id: document.getElementById("te-exec-id").value.trim(),
        };
        try {
          const response = await fetch("/api/agents", {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify(payload),
          });
          const data = await response.json();
          if (!response.ok) throw new Error(data.detail || "request failed");
          showResult(data);
          statusEl.className = "status ok";
          statusEl.textContent = "Created. Copy the token (and any private key) now \\u2014 shown once.";
        } catch (error) {
          statusEl.className = "status error";
          statusEl.textContent = error.message || "request failed";
        } finally {
          submit.disabled = false;
        }
      });
    }

    function wireSkill() {
      const form = document.getElementById("skill-form");
      const submit = document.getElementById("skill-submit");
      const statusEl = document.getElementById("skill-status");
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        submit.disabled = true;
        statusEl.className = "status";
        statusEl.textContent = "";
        const payload = {
          agent_id: document.getElementById("skill-agent-id").value.trim(),
          council_id: document.getElementById("skill-council-id").value,
          council_endpoint: document.getElementById("skill-endpoint").value.trim(),
          mode: document.querySelector('input[name="mode"]:checked').value,
          has_trusted_executor: document.getElementById("skill-te").checked,
        };
        try {
          const response = await fetch("/api/skill", {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify(payload),
          });
          if (!response.ok) {
            let detail = "request failed";
            try { detail = (await response.json()).detail || detail; } catch (_e) { /* ignore */ }
            throw new Error(detail);
          }
          const blob = await response.blob();
          const url = URL.createObjectURL(blob);
          const a = document.createElement("a");
          a.href = url;
          a.download = "SKILL.md";
          a.click();
          URL.revokeObjectURL(url);
          statusEl.className = "status ok";
          statusEl.textContent = "Downloaded SKILL.md.";
        } catch (error) {
          statusEl.className = "status error";
          statusEl.textContent = error.message || "request failed";
        } finally {
          submit.disabled = false;
        }
      });
    }

    setupTabs();
    setupTeChoice();
    setupCopyButtons();
    wireSignup();
    wireSkill();
  </script>
</body>
</html>"""
    return (
        html_doc.replace("__ROLE_OPTIONS__", role_options)
        .replace("__COUNCIL_OPTIONS__", council_options)
        .replace("__ESCAPED_ENDPOINT__", escaped_endpoint)
    )


def _render_admin() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Nomos Account Admin</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f8fb;
      --panel: #ffffff;
      --ink: #182235;
      --muted: #5d6b81;
      --line: #d9e1ec;
      --blue: #2457d6;
      --green: #0f8a5f;
      --red: #c93c37;
      --amber: #a36300;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font: 14px/1.45 Inter, ui-sans-serif, system-ui, -apple-system,
        BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--ink);
      background: var(--bg);
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 20px clamp(18px, 4vw, 46px);
      background: #132039;
      color: #fff;
    }
    h1 { margin: 0; font-size: 24px; letter-spacing: 0; }
    main {
      display: grid;
      grid-template-columns: minmax(420px, 1fr) minmax(320px, 420px);
      gap: 18px;
      padding: 22px clamp(18px, 4vw, 46px);
      align-items: start;
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: 0 10px 24px rgba(20, 36, 70, 0.05);
    }
    .section-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
    }
    h2 { margin: 0; font-size: 17px; }
    .table-wrap { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; min-width: 720px; }
    th, td {
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: middle;
    }
    th { color: var(--muted); font-size: 12px; text-transform: uppercase; }
    tr { cursor: pointer; }
    tr:hover, tr.selected { background: #eef4ff; }
    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      border-radius: 999px;
      padding: 0 9px;
      background: #edf2fa;
      color: #2b3a50;
      font-size: 12px;
      font-weight: 800;
    }
    .pill.off { background: #f7ecec; color: var(--red); }
    form { padding: 16px; }
    label { display: block; margin: 12px 0 6px; font-weight: 800; }
    input, select, textarea {
      width: 100%;
      min-height: 40px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px 10px;
      font: inherit;
      color: var(--ink);
      background: #fff;
    }
    textarea { resize: vertical; min-height: 72px; }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px; }
    button {
      min-height: 38px;
      border: 0;
      border-radius: 6px;
      padding: 0 12px;
      background: var(--blue);
      color: white;
      font: inherit;
      font-weight: 800;
      cursor: pointer;
    }
    button.secondary { background: #e7edf6; color: var(--ink); }
    button.danger { background: var(--red); }
    button:disabled { opacity: .55; cursor: not-allowed; }
    code {
      display: block;
      padding: 10px;
      border-radius: 6px;
      border: 1px solid var(--line);
      background: #f8fafc;
      overflow-wrap: anywhere;
      white-space: pre-wrap;
    }
    .status { padding: 0 16px 16px; min-height: 24px; font-weight: 800; }
    .status.ok { color: var(--green); }
    .status.error { color: var(--red); }
    .muted { color: var(--muted); }
    @media (max-width: 920px) {
      main { grid-template-columns: 1fr; }
      .row { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <h1>Nomos Account Admin</h1>
    <button id="refresh" class="secondary" type="button">Refresh</button>
  </header>
  <main>
    <section>
      <div class="section-head">
        <h2>Agent Accounts</h2>
        <span id="count" class="muted">0 accounts</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Agent ID</th>
              <th>Name</th>
              <th>Role</th>
              <th>State</th>
              <th>Councils</th>
              <th>Token</th>
            </tr>
          </thead>
          <tbody id="accounts"></tbody>
        </table>
      </div>
    </section>
    <section>
      <div class="section-head">
        <h2>Edit Account</h2>
        <span id="selected-label" class="muted">Select an account</span>
      </div>
      <form id="edit-form">
        <label for="agent-id">Agent ID</label>
        <input id="agent-id" disabled>
        <div class="row">
          <div>
            <label for="display-name">Display Name</label>
            <input id="display-name" maxlength="120">
          </div>
          <div>
            <label for="role">Role</label>
            <select id="role">
              <option value="readonly">readonly</option>
              <option value="developer">developer</option>
              <option value="operator">operator</option>
              <option value="admin">admin</option>
            </select>
          </div>
        </div>
        <label for="enabled">State</label>
        <select id="enabled">
          <option value="true">enabled</option>
          <option value="false">disabled</option>
        </select>
        <label for="councils">Councils</label>
        <textarea id="councils" spellcheck="false"></textarea>
        <label for="current-token">Token</label>
        <code id="current-token"></code>
        <div class="actions">
          <button id="save" type="submit" disabled>Save</button>
          <button id="rotate" class="secondary" type="button" disabled>Rotate Token</button>
          <button id="delete" class="danger" type="button" disabled>Delete</button>
        </div>
      </form>
      <div id="status" class="status" aria-live="polite"></div>
      <div id="token-output" hidden style="padding: 0 16px 16px;">
        <label>New Token</label>
        <code id="new-token"></code>
      </div>
    </section>
    <section>
      <div class="section-head">
        <h2>Signup Councils</h2>
        <span class="muted">Public form choices</span>
      </div>
      <form id="config-form">
        <label for="allowed-councils">Allowed Councils</label>
        <textarea id="allowed-councils" spellcheck="false"></textarea>
        <label for="default-council">Default Council</label>
        <input id="default-council" maxlength="120">
        <div class="actions">
          <button id="save-config" type="submit">Save Councils</button>
        </div>
      </form>
      <div id="config-status" class="status" aria-live="polite"></div>
    </section>
  </main>
  <script>
    const tbody = document.getElementById("accounts");
    const count = document.getElementById("count");
    const statusEl = document.getElementById("status");
    const selectedLabel = document.getElementById("selected-label");
    const tokenBox = document.getElementById("token-output");
    const newToken = document.getElementById("new-token");
    const form = document.getElementById("edit-form");
    const configForm = document.getElementById("config-form");
    const configStatus = document.getElementById("config-status");
    const controls = {
      agentId: document.getElementById("agent-id"),
      displayName: document.getElementById("display-name"),
      role: document.getElementById("role"),
      enabled: document.getElementById("enabled"),
      councils: document.getElementById("councils"),
      currentToken: document.getElementById("current-token"),
      save: document.getElementById("save"),
      rotate: document.getElementById("rotate"),
      delete: document.getElementById("delete"),
    };
    const configControls = {
      allowedCouncils: document.getElementById("allowed-councils"),
      defaultCouncil: document.getElementById("default-council"),
    };
    let accounts = [];
    let selected = null;

    function setStatus(message, ok = true) {
      statusEl.className = ok ? "status ok" : "status error";
      statusEl.textContent = message;
    }

    function setConfigStatus(message, ok = true) {
      configStatus.className = ok ? "status ok" : "status error";
      configStatus.textContent = message;
    }

    async function api(path, options = {}) {
      const response = await fetch(path, {
        ...options,
        headers: { "content-type": "application/json", ...(options.headers || {}) },
      });
      if (response.status === 204) {
        return null;
      }
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.detail || "request failed");
      }
      return data;
    }

    function renderRows() {
      tbody.replaceChildren();
      count.textContent = `${accounts.length} account${accounts.length === 1 ? "" : "s"}`;
      for (const account of accounts) {
        const row = document.createElement("tr");
        if (selected && selected.agent_id === account.agent_id) {
          row.className = "selected";
        }
        const cells = [
          account.agent_id,
          account.display_name,
          account.role,
          account.enabled ? "enabled" : "disabled",
          account.councils.join(", "),
          account.auth_token ? "stored" : "not stored",
        ];
        for (const value of cells) {
          const cell = document.createElement("td");
          cell.textContent = value;
          row.appendChild(cell);
        }
        row.children[3].innerHTML = "";
        const state = document.createElement("span");
        state.className = account.enabled ? "pill" : "pill off";
        state.textContent = account.enabled ? "enabled" : "disabled";
        row.children[3].appendChild(state);
        row.addEventListener("click", () => selectAccount(account));
        tbody.appendChild(row);
      }
    }

    function selectAccount(account) {
      selected = account;
      selectedLabel.textContent = account.agent_id;
      controls.agentId.value = account.agent_id;
      controls.displayName.value = account.display_name;
      controls.role.value = account.role;
      controls.enabled.value = account.enabled ? "true" : "false";
      controls.councils.value = account.councils.join("\\n");
      controls.currentToken.textContent = account.auth_token
        || "Not available for this account. Rotate the token to display a new one once.";
      controls.save.disabled = false;
      controls.rotate.disabled = false;
      controls.delete.disabled = false;
      renderRows();
    }

    async function loadAccounts() {
      try {
        const data = await api("/api/admin/accounts");
        accounts = data.accounts;
        if (selected) {
          selected = accounts.find((account) => account.agent_id === selected.agent_id) || null;
        }
        renderRows();
        if (selected) {
          selectAccount(selected);
        }
        setStatus("Loaded.");
      } catch (error) {
        setStatus(error.message || "request failed", false);
      }
    }

    async function loadConfig() {
      try {
        const data = await api("/api/admin/config");
        configControls.allowedCouncils.value = data.allowed_councils.join("\\n");
        configControls.defaultCouncil.value = data.default_council_id;
        setConfigStatus("Loaded.");
      } catch (error) {
        setConfigStatus(error.message || "request failed", false);
      }
    }

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!selected) return;
      const payload = {
        display_name: controls.displayName.value,
        role: controls.role.value,
        enabled: controls.enabled.value === "true",
        councils: controls.councils.value.split(/\\s*,\\s*|\\n/)
          .map((value) => value.trim())
          .filter(Boolean),
      };
      try {
        selected = await api(`/api/admin/accounts/${encodeURIComponent(selected.agent_id)}`, {
          method: "PATCH",
          body: JSON.stringify(payload),
        });
        await loadAccounts();
        setStatus("Saved.");
      } catch (error) {
        setStatus(error.message || "request failed", false);
      }
    });

    controls.rotate.addEventListener("click", async () => {
      if (!selected) return;
      try {
        const data = await api(
          `/api/admin/accounts/${encodeURIComponent(selected.agent_id)}/rotate-token`,
          { method: "POST" },
        );
        selected = data.account;
        newToken.textContent = data.auth_token;
        await loadAccounts();
        newToken.textContent = data.auth_token;
        tokenBox.hidden = false;
        setStatus("Token rotated.");
      } catch (error) {
        setStatus(error.message || "request failed", false);
      }
    });

    configForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const payload = {
        default_council_id: configControls.defaultCouncil.value.trim(),
        allowed_councils: configControls.allowedCouncils.value.split(/\\s*,\\s*|\\n/)
          .map((value) => value.trim())
          .filter(Boolean),
      };
      try {
        const data = await api("/api/admin/config", {
          method: "PATCH",
          body: JSON.stringify(payload),
        });
        configControls.allowedCouncils.value = data.allowed_councils.join("\\n");
        configControls.defaultCouncil.value = data.default_council_id;
        setConfigStatus("Saved.");
      } catch (error) {
        setConfigStatus(error.message || "request failed", false);
      }
    });

    controls.delete.addEventListener("click", async () => {
      if (!selected) return;
      if (!window.confirm(`Delete ${selected.agent_id}?`)) return;
      try {
        await api(`/api/admin/accounts/${encodeURIComponent(selected.agent_id)}`, {
          method: "DELETE",
        });
        selected = null;
        controls.agentId.value = "";
        controls.displayName.value = "";
        controls.currentToken.textContent = "";
        controls.save.disabled = true;
        controls.rotate.disabled = true;
        controls.delete.disabled = true;
        tokenBox.hidden = true;
        await loadAccounts();
        setStatus("Deleted.");
      } catch (error) {
        setStatus(error.message || "request failed", false);
      }
    });

    document.getElementById("refresh").addEventListener("click", loadAccounts);
    loadAccounts();
    loadConfig();
  </script>
</body>
</html>"""


# ── trusted executors ──────────────────────────────────────────────────


def fingerprint_from_pubkey_hex(public_key_hex: str) -> tuple[str, str]:
    """Return (normalized_hex, key_fingerprint). Fingerprint = sha256(raw 32B key),
    matching the council's recompute and the executor's ExecutorIdentity. Mirrors
    scripts/agent_accounts.py::fingerprint_from_pubkey_hex but raises HTTP 422."""
    cleaned = public_key_hex.strip().lower()
    try:
        raw = bytes.fromhex(cleaned)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"public key is not valid hex: {exc}",
        ) from exc
    if len(raw) != 32:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"public key must decode to 32 bytes (ed25519); got {len(raw)}",
        )
    return cleaned, hashlib.sha256(raw).hexdigest()


def validate_executor_id(executor_id: str) -> str:
    if not executor_id or not EXECUTOR_ID_RE.fullmatch(executor_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="executor_id must contain only letters, numbers, and _ : . -",
        )
    return executor_id


class _RedisTrustedExecutors:
    """Raw-redis trusted-executor target mirroring RedisStore's key layout, so the
    dev/redis path stays byte-identical to what the executor/RedisStore write."""

    def __init__(self, client: aioredis.Redis, prefix: str):
        self._c = client
        self._index = f"{prefix}trusted_executors"
        self._pfx = f"{prefix}trusted_executors:"

    async def register_trusted_executor(
        self,
        *,
        executor_id: str,
        public_key_hex: str,
        key_fingerprint: str,
        metadata: dict[str, Any] | None = None,
        enabled: bool = True,
    ) -> None:
        key = self._pfx + key_fingerprint
        existing = await self._c.hgetall(key)
        now = str(time.time())
        await self._c.hset(
            key,
            mapping={
                "executor_id": executor_id,
                "public_key_hex": public_key_hex,
                "key_fingerprint": key_fingerprint,
                "enabled": "1" if enabled else "0",
                "registered_at": existing.get("registered_at", now),
                "updated_at": now,
                "metadata_json": json.dumps(metadata or {}, sort_keys=True),
            },
        )
        await self._c.sadd(self._index, key_fingerprint)

    async def get_trusted_executor(self, key_fingerprint: str) -> dict[str, str] | None:
        data = await self._c.hgetall(self._pfx + key_fingerprint)
        return data or None


async def _resolve_and_register_te(store: Any, request: SignupRequest) -> dict[str, str]:
    """Resolve the trusted-executor public key (paste or server-side generate),
    register pubkey→id into the durable store, and return the response fields. The
    private key (generate only) is returned once and never persisted."""
    private_key_hex = ""
    if request.te_mode == "generate":
        from nomos.level_0.crypto.signing import KeyPair

        keypair = KeyPair.generate()
        public_key_hex = keypair.public_key_hex()
        private_key_hex = keypair.private_key.hex()
    elif request.te_mode == "paste":
        if not request.te_public_key_hex.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="te_public_key_hex is required when te_mode='paste'",
            )
        public_key_hex = request.te_public_key_hex
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="te_mode must be 'paste' or 'generate' when has_trusted_executor is set",
        )

    cleaned_hex, key_fingerprint = fingerprint_from_pubkey_hex(public_key_hex)
    executor_id = request.te_executor_id or f"agent:{key_fingerprint[:16]}"
    validate_executor_id(executor_id)
    await store.register_trusted_executor(
        executor_id=executor_id,
        public_key_hex=cleaned_hex,
        key_fingerprint=key_fingerprint,
        metadata={"source": "onboarding", "agent_id": request.agent_id},
        enabled=True,
    )
    record = await store.get_trusted_executor(key_fingerprint)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="trusted executor registration read-back failed",
        )
    fields = {
        "te_executor_id": executor_id,
        "te_public_key_hex": cleaned_hex,
        "te_key_fingerprint": key_fingerprint,
    }
    if private_key_hex:
        fields["te_private_key_hex"] = private_key_hex
    return fields


async def create_agent_account_pg(
    store: Any,
    settings: SignupSettings,
    request: SignupRequest,
) -> SignupResponse:
    """Service-mode account creation: write to the durable store the council reads
    (PostgreSQL), via the same methods as the PG account-register job."""
    signup_config = await get_signup_config(settings)
    council_id = request.council_id or signup_config.default_council_id
    if council_id not in signup_config.allowed_councils:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"council_id {council_id!r} is not allowed by this signup service",
        )
    role = request.role or settings.default_role
    if role not in settings.allowed_roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"role {role!r} is not allowed by this signup service",
        )
    permissions = permissions_for_role(role)
    display_name = request.display_name or request.agent_id
    te_fields: dict[str, str] = {}

    try:
        # nomos.auth.provisioning owns the write sequence, its ordering (token
        # index published last so a partial write cannot authenticate), and the
        # post-write integrity check. This console previously reimplemented all
        # three, which is how the orderings drifted apart.
        created = await provisioning.create_account(
            store,
            agent_id=request.agent_id,
            role=role,
            display_name=display_name,
            councils=[council_id],
        )
        raw_token = created["token"]
    except provisioning.AccountExistsError as exc:
        # A distinct type, not a substring match on the message: "the caller asked
        # for something already there" (409) and "we could not write" (500) are
        # different answers, and deciding between them by text meant a reworded
        # message would silently turn a conflict into an internal error.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="agent_id already exists; choose a different id",
        ) from exc
    except provisioning.ProvisioningError as exc:
        # Do NOT return str(exc). Provisioning messages describe internal state --
        # store backend, integrity-key configuration, driver errors -- and this
        # endpoint is reachable by anyone who can reach the signup console.
        _log_signup_failure("account provisioning failed", request.agent_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="could not create agent account",
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="could not create agent account",
        ) from exc

    # Trusted-executor registration runs AFTER the account is published. At this
    # point the account is fully authenticable while its raw token exists only in
    # the response we are about to abandon, so failing here without rolling back
    # would leave a live credential nobody holds.
    if request.has_trusted_executor:
        try:
            te_fields = await _resolve_and_register_te(store, request)
        except Exception as exc:
            try:
                await provisioning.delete_account(store, agent_id=request.agent_id)
            except Exception as rollback_exc:  # pragma: no cover - best effort
                _log_signup_failure(
                    "ORPHANED ACCOUNT: trusted-executor registration failed and "
                    "rollback did not complete; the account is live and its token "
                    "was never returned",
                    request.agent_id,
                    rollback_exc,
                )
            if isinstance(exc, HTTPException):
                raise
            _log_signup_failure("trusted-executor registration failed", request.agent_id, exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="could not register trusted executor",
            ) from exc

    return SignupResponse(
        agent_id=request.agent_id,
        display_name=display_name,
        council_endpoint=settings.council_endpoint,
        council_id=council_id,
        role=role,
        permissions=list(permissions),
        auth_token=raw_token,
        **te_fields,
    )


# ── SKILL.md generation ─────────────────────────────────────────────────
# The council-reasoning guidance below is condensed from skills/council-protocol/
# SKILL.md (not shipped in this image), so it is embedded as a constant.

_SKILL_PROTOCOL_BODY = """## What the council reads

The members reason about the *content* of this one intent — there is no memory
between calls. Make these fields truthful and specific:

- **`command`** — the exact string that will run (the writ binds it).
- **`description`** — plain English for what the command does; must match `command`.
- **`target_resource`** — the specific thing acted on (`k8s://prod/api-gateway`, not "the cluster").
- **`target_environment`** — `development` / `staging` / `production` / `dr`. Be honest.
- **`justification`** — why this, why now (tickets, alerts, runbooks ground it).
- **`blast_radius`** — what could break, how widely. Underclaiming is a common deny reason.
- **`evidence`** (≤20) — log lines, command output, observed state.
- **`agent_id`** + **`auth_token`** — your identity and proof of it.

## Reading the response

- **`status`** — `approved` / `denied` / `error`.
- **`guidance`** — concrete fixes when denied.
- **`missing_permissions`** — scopes your role lacks.
- **`council_members[].reasoning`** — each member's objection; read all of them.
- **`chairman.risk_level`** — `low` / `medium` / `high` / `critical`.
- On `approved`, submit the returned **`writ`** to the trusted executor.

## When to revise vs. accept

Two paths from a denial:

- **Revise** when the denial is about *framing* — vague target, thin justification,
  underclaimed blast radius, missing evidence. Address each concern and resubmit **once**.
  A second denial after an honest revision means stop.
- **Accept** when the denial is about *scope / authority* ("not within this agent's
  responsibilities", "requires human review", missing permissions categorically not in
  your role). Stop, surface the chairman + a member's reasoning verbatim, and suggest the
  right next step (human in the loop, different role, change ticket).

Don't substitute an equivalent command, decompose the action into smaller pieces, or
re-ask through the other tool — each call is judged on its own content, so the verdict
won't change. `error` on a bad/missing `auth_token` is not a denial: fix the token, don't retry blindly.
"""

_SKILL_CERTIFIED_EVIDENCE = """## Certified evidence (trusted executor)

Your trusted executor signs an `ExecutionEvidence` payload after running a command. When
you submit a follow-up intent, include that signed evidence in `certified_evidence` so the
council can verify what actually happened. The council validates the signature against the
executor key registered for you during onboarding — no extra setup is needed on your side.
"""


def _skill_intro(agent_id: str, council_id: str, endpoint: str) -> str:
    return f"""# Routing actions through the Nomos council

You are **{agent_id}**. Privileged actions (shell commands, deploys, file writes) must be
**authorized by the Nomos Security Council before you run them** — never execute such
actions directly. The council is a multi-member LLM jury that reviews each intent and, on
approval, mints a cryptographically signed *writ* the trusted executor will honor.

- **Council:** `{council_id}`
- **Endpoint:** `{endpoint}`
- **Auth:** your account token, provided to your runtime as `NOMOS_AUTH_TOKEN`
  (never hard-code it into this file).
"""


def _skill_tool_use(agent_id: str, council_id: str) -> str:
    example = (
        (
            "```json\n"
            "{\n"
            '  "agent_id": "__AGENT__",\n'
            '  "auth_token": "$NOMOS_AUTH_TOKEN",\n'
            '  "council_id": "__COUNCIL__",\n'
            '  "command": "kubectl rollout restart deployment/api-gateway -n prod",\n'
            '  "working_directory": "/home/agent",\n'
            '  "description": "Restart api-gateway after cert rotation, per runbook RB-0042.",\n'
            '  "target_resource": "k8s://prod/api-gateway",\n'
            '  "target_environment": "production",\n'
            '  "justification": "Cert rotation completed; pods need a restart.",\n'
            '  "blast_radius": "Brief 503s during the rolling restart (~30s)."\n'
            "}\n"
            "```"
        )
        .replace("__AGENT__", agent_id)
        .replace("__COUNCIL__", council_id)
    )
    return f"""## Mode A — tool use (council tools registered)

Your runtime exposes the council as MCP tools. Two matter:

- `council_propose_intent` — dry run: same evaluation, no writ minted. Use when unsure.
- `council_authorize` — commit: on approval returns a signed **writ** for the trusted executor.

Call `council_authorize` with arguments like:

{example}
"""


def _skill_mcp(agent_id: str, council_id: str, endpoint: str) -> str:
    example = (
        (
            "```python\n"
            "from mcp import ClientSession\n"
            "from mcp.client.streamable_http import streamablehttp_client\n"
            "\n"
            'async with streamablehttp_client("__ENDPOINT__") as (read, write, _):\n'
            "    async with ClientSession(read, write) as session:\n"
            "        await session.initialize()\n"
            '        result = await session.call_tool("council_authorize", arguments={\n'
            '            "agent_id": "__AGENT__",\n'
            '            "auth_token": "<NOMOS_AUTH_TOKEN>",\n'
            '            "council_id": "__COUNCIL__",\n'
            '            "command": "…",\n'
            '            "working_directory": "/home/agent",\n'
            '            "description": "…",\n'
            '            "target_resource": "…",\n'
            "        })\n"
            "```"
        )
        .replace("__ENDPOINT__", endpoint)
        .replace("__AGENT__", agent_id)
        .replace("__COUNCIL__", council_id)
    )
    return f"""## Mode B — direct MCP

Open a streamable-HTTP MCP session to the council `/mcp` endpoint (`{endpoint}`) and call
its tools directly. `council_id` (`{council_id}`) is required by the v2 tools.

1. `initialize` the session.
2. `tools/list` — `council_propose_intent`, `council_authorize`, `council_check_status`, …
3. `tools/call` with the same argument object as Mode A.

{example}
"""


def build_skill_md(settings: SignupSettings, request: SkillRequest) -> str:
    council_id = request.council_id or settings.default_council_id
    endpoint = request.council_endpoint or settings.council_endpoint
    sections = [_skill_intro(request.agent_id, council_id, endpoint)]
    if request.mode in ("tool-use", "both"):
        sections.append(_skill_tool_use(request.agent_id, council_id))
    if request.mode in ("mcp", "both"):
        sections.append(_skill_mcp(request.agent_id, council_id, endpoint))
    if request.has_trusted_executor:
        sections.append(_SKILL_CERTIFIED_EVIDENCE)
    sections.append(_SKILL_PROTOCOL_BODY)
    return "\n".join(section.strip("\n") + "\n" for section in sections)


def _render_council(settings: SignupSettings) -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Nomos Council Creation</title>
  <style>
    :root { color-scheme: light; --bg: #eef2f8; --panel: #fff; --ink: #0e1726;
      --muted: #66748b; --line: #d6deeb; --accent: #1f4fd4; --accent-deep: #0d2b8a; }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; color: var(--ink);
      font: 14.5px/1.55 Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
      background: linear-gradient(180deg, #f6f8fc, #e6ecf6); }
    header { padding: 28px clamp(18px,5vw,64px) 22px; color: #fff;
      background: linear-gradient(135deg, #0c1530 0%, #16244b 55%, #1f4fd4 130%); }
    .brand { font-size: 12px; letter-spacing: .18em; text-transform: uppercase;
      color: rgba(255,255,255,.65); }
    h1 { margin: 8px 0 4px; font-size: clamp(26px,4vw,38px); }
    header p { margin: 0; color: rgba(255,255,255,.75); max-width: 62ch; }
    main { padding: 28px clamp(18px,5vw,64px) 56px; max-width: 760px; }
    section { background: var(--panel); border: 1px solid var(--line); border-radius: 14px;
      box-shadow: 0 10px 30px rgba(15,30,70,.08); overflow: hidden; }
    .panel-head { padding: 18px 20px 12px; border-bottom: 1px solid #e7ecf4; }
    .panel-head h2 { margin: 0; font-size: 17px; }
    .panel-head .subtle { margin: 4px 0 0; color: var(--muted); font-size: 13px; }
    .panel-body { padding: 18px 20px 22px; }
    label { display: block; margin: 14px 0 6px; font-weight: 600; font-size: 13px; }
    input, select, textarea { width: 100%; min-height: 42px; border: 1px solid var(--line);
      border-radius: 8px; padding: 9px 12px; font: inherit; background: #fff; }
    .note { margin-top: 16px; padding: 12px 14px; border-radius: 10px;
      background: #fff7e6; border: 1px solid #f3d28a; color: #7a5200; font-size: 13px; }
    button { margin-top: 18px; min-height: 44px; width: 100%; border: 0; border-radius: 9px;
      background: linear-gradient(180deg, var(--accent), var(--accent-deep)); color: #fff;
      font: inherit; font-weight: 700; cursor: not-allowed; opacity: .6; }
  </style>
</head>
<body>
  <header>
    <div class="brand">Nomos Security Council</div>
    <h1>Create a Council</h1>
    <p>Stand up a new council profile from a server-side template.</p>
  </header>
  <main>
    <section>
      <div class="panel-head">
        <h2>New Council</h2>
        <p class="subtle">Coming soon &mdash; this page is a stub.</p>
      </div>
      <div class="panel-body">
        <form id="council-form" onsubmit="return false;">
          <label for="council-id">Council ID</label>
          <input id="council-id" placeholder="council-team-x" autocomplete="off">
          <label for="template">Template</label>
          <select id="template">
            <option value="strict-production">strict-production</option>
            <option value="default">default</option>
            <option value="git-workflow-local">git-workflow-local</option>
          </select>
          <label for="description">Description</label>
          <input id="description" placeholder="What this council governs">
          <label for="use-case">Use case</label>
          <input id="use-case" placeholder="Intended use">
          <button type="submit" disabled>Create council (not yet enabled)</button>
        </form>
        <div class="note">
          Council creation is not yet wired. When enabled, this will call the council MCP
          tool <code>council_create_council</code> with a management token; dynamic council
          mutations always go through the council, never a direct database write.
        </div>
      </div>
    </section>
  </main>
</body>
</html>"""


app = create_app()
