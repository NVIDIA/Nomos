# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Small local service for creating Nomos agent accounts."""

# ruff: noqa: E501
# The embedded HTML/CSS/JS template keeps browser-facing markup readable here.

from __future__ import annotations

import hashlib
import html
import os
import re
import secrets
import time
from dataclasses import dataclass
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, HTTPException, Response, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field, field_validator

from nomos.auth.account_integrity import ACCOUNT_INTEGRITY_FIELD, sign_account_record
from nomos.auth.permissions import PermissionScope
from nomos_runtime import SecretCheck, validate_deployment_preflight
from nomos_runtime.redis_client import RedisConnectionConfig, create_redis_client

AGENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
COUNCIL_ID_RE = re.compile(r"^[A-Za-z0-9_*.-]+$")

ALL_PERMISSIONS = tuple(scope.value for scope in PermissionScope)
ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "readonly": (
        PermissionScope.SHELL_READ.value,
        PermissionScope.FILE_READ.value,
    ),
    "developer": (
        PermissionScope.SHELL_READ.value,
        PermissionScope.SHELL_WRITE.value,
        PermissionScope.FILE_READ.value,
        PermissionScope.FILE_WRITE.value,
    ),
    "operator": (
        PermissionScope.SHELL_READ.value,
        PermissionScope.SHELL_WRITE.value,
        PermissionScope.FILE_READ.value,
        PermissionScope.FILE_WRITE.value,
        PermissionScope.INFRA_DEPLOY.value,
    ),
    "admin": ALL_PERMISSIONS,
}


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


class SignupResponse(BaseModel):
    agent_id: str
    display_name: str
    council_endpoint: str
    council_id: str
    role: str
    permissions: list[str]
    auth_token: str


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
        account_integrity_key=os.environ.get("NOMOS_ACCOUNT_INTEGRITY_KEY", ""),
    )
    validate_settings(settings)
    return settings


def create_app(settings: SignupSettings | None = None) -> FastAPI:
    if os.environ.get("COUNCIL_STORAGE_MODE", "redis").strip().lower() == "postgres":
        # Fail loudly rather than writing accounts to a store the council no
        # longer reads. This dev-only service has not been ported to the
        # DurableStore API yet (the council reads accounts from PostgreSQL in
        # this mode); use scripts/agent_accounts.py guidance or redis mode.
        raise RuntimeError(
            "agent_signup does not support COUNCIL_STORAGE_MODE=postgres yet; "
            "accounts written here would be invisible to the council"
        )
    app_settings = settings or settings_from_env()
    validate_settings(app_settings)
    app = FastAPI(title="Nomos Agent Signup", version="0.1.0")
    security = HTTPBasic()

    def require_admin(credentials: HTTPBasicCredentials = Depends(security)) -> None:
        username_ok = secrets.compare_digest(credentials.username, app_settings.admin_user)
        password_ok = secrets.compare_digest(credentials.password, app_settings.admin_password)
        if not (username_ok and password_ok):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="admin authentication required",
                headers={"WWW-Authenticate": "Basic"},
            )

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return _render_index(app_settings, await get_signup_config(app_settings))

    @app.get("/admin", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
    async def admin() -> str:
        return _render_admin()

    @app.get("/healthz", response_model=HealthResponse)
    async def healthz(response: Response) -> HealthResponse:
        try:
            client = redis_client(app_settings)
            try:
                await client.ping()
            finally:
                await client.aclose()
        except Exception:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return HealthResponse(ok=False, redis="unreachable")
        return HealthResponse(ok=True, redis="ok")

    @app.post(
        "/api/agents",
        response_model=SignupResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_agent(request: SignupRequest) -> SignupResponse:
        return await create_agent_account(app_settings, request)

    @app.get(
        "/api/admin/accounts",
        response_model=AccountListResponse,
        dependencies=[Depends(require_admin)],
    )
    async def list_admin_accounts() -> AccountListResponse:
        return AccountListResponse(accounts=await list_agent_accounts(app_settings))

    @app.get(
        "/api/admin/config",
        response_model=SignupConfig,
        dependencies=[Depends(require_admin)],
    )
    async def get_admin_config() -> SignupConfig:
        return await get_signup_config(app_settings)

    @app.patch(
        "/api/admin/config",
        response_model=SignupConfig,
        dependencies=[Depends(require_admin)],
    )
    async def patch_admin_config(request: SignupConfigUpdate) -> SignupConfig:
        return await update_signup_config(app_settings, request.to_config())

    @app.get(
        "/api/admin/accounts/{agent_id}",
        response_model=AgentAccountRecord,
        dependencies=[Depends(require_admin)],
    )
    async def get_admin_account(agent_id: str) -> AgentAccountRecord:
        account = await get_agent_account(app_settings, agent_id)
        if account is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="agent not found")
        return account

    @app.patch(
        "/api/admin/accounts/{agent_id}",
        response_model=AgentAccountRecord,
        dependencies=[Depends(require_admin)],
    )
    async def patch_admin_account(
        agent_id: str,
        request: AccountUpdateRequest,
    ) -> AgentAccountRecord:
        return await update_agent_account(app_settings, agent_id, request)

    @app.post(
        "/api/admin/accounts/{agent_id}/rotate-token",
        response_model=TokenRotateResponse,
        dependencies=[Depends(require_admin)],
    )
    async def rotate_admin_token(agent_id: str) -> TokenRotateResponse:
        return await rotate_agent_token(app_settings, agent_id)

    @app.delete(
        "/api/admin/accounts/{agent_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_admin)],
    )
    async def delete_admin_account(agent_id: str) -> Response:
        await delete_agent_account(app_settings, agent_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return app


def redis_client(settings: SignupSettings) -> aioredis.Redis:
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
    try:
        return ROLE_PERMISSIONS[role]
    except KeyError as exc:
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
    html_doc = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Nomos Agent Signup</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #eef2f8;
      --bg-grad-a: #f6f8fc;
      --bg-grad-b: #e6ecf6;
      --panel: #ffffff;
      --panel-soft: #f7f9fc;
      --ink: #0e1726;
      --ink-soft: #2a3a55;
      --muted: #66748b;
      --line: #d6deeb;
      --line-soft: #e7ecf4;
      --accent: #1f4fd4;
      --accent-deep: #0d2b8a;
      --accent-soft: #e7eeff;
      --green: #0a7a52;
      --red: #c0392f;
      --code-bg: #0f172a;
      --code-ink: #e3ebff;
      --code-line: #1b2a4a;
      --code-comment: #7e8fb8;
      --shadow-sm: 0 1px 2px rgba(15, 23, 42, 0.04);
      --shadow-md: 0 10px 30px rgba(15, 30, 70, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; }}
    body {{
      min-height: 100vh;
      font: 14.5px/1.55 Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
        "Segoe UI", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(1100px 600px at 100% -10%, rgba(31, 79, 212, 0.10), transparent 60%),
        radial-gradient(900px 500px at -10% 110%, rgba(13, 43, 138, 0.07), transparent 60%),
        linear-gradient(180deg, var(--bg-grad-a), var(--bg-grad-b));
    }}
    header {{
      padding: 28px clamp(18px, 5vw, 64px) 22px;
      background: linear-gradient(135deg, #0c1530 0%, #16244b 55%, #1f4fd4 130%);
      color: #fff;
      border-bottom: 1px solid #0a1226;
      position: relative;
      overflow: hidden;
    }}
    header::after {{
      content: "";
      position: absolute;
      left: 0; right: 0; bottom: 0; height: 2px;
      background: linear-gradient(90deg, transparent, #4a7bff 30%, #4a7bff 70%, transparent);
      opacity: 0.6;
    }}
    .brand {{
      display: flex; align-items: center; gap: 12px;
      font-size: 12px; letter-spacing: 0.18em; text-transform: uppercase;
      color: rgba(255,255,255,0.65);
    }}
    .brand .dot {{
      width: 8px; height: 8px; border-radius: 50%;
      background: #4cc38a; box-shadow: 0 0 0 4px rgba(76,195,138,0.18);
    }}
    h1 {{
      margin: 8px 0 4px;
      font-size: clamp(28px, 4vw, 40px);
      font-weight: 700;
      letter-spacing: -0.01em;
    }}
    header p {{
      margin: 0;
      color: rgba(255,255,255,0.75);
      max-width: 62ch;
      font-size: 15px;
    }}
    main {{
      display: grid;
      grid-template-columns: minmax(280px, 380px) minmax(320px, 1fr);
      gap: 22px;
      padding: 28px clamp(18px, 5vw, 64px) 56px;
      align-items: start;
    }}
    section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 14px;
      box-shadow: var(--shadow-md);
      overflow: hidden;
    }}
    .panel-head {{
      padding: 18px 20px 12px;
      border-bottom: 1px solid var(--line-soft);
    }}
    .panel-head h2 {{ margin: 0; font-size: 17px; font-weight: 700; letter-spacing: -0.005em; }}
    .panel-head .subtle {{ margin: 4px 0 0; color: var(--muted); font-size: 13px; }}
    .panel-body {{ padding: 18px 20px 20px; }}
    label {{
      display: block; margin: 14px 0 6px;
      font-weight: 600; font-size: 13px; color: var(--ink-soft);
    }}
    label:first-of-type {{ margin-top: 4px; }}
    input, select {{
      width: 100%; min-height: 42px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 9px 12px;
      font: inherit;
      color: var(--ink);
      background: #fff;
      transition: border-color .15s, box-shadow .15s;
    }}
    input:focus, select:focus {{
      outline: none;
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(31,79,212,0.18);
    }}
    button {{
      margin-top: 18px;
      min-height: 44px;
      width: 100%;
      border: 0;
      border-radius: 9px;
      padding: 0 16px;
      background: linear-gradient(180deg, var(--accent), var(--accent-deep));
      color: white;
      font: inherit;
      font-weight: 700;
      letter-spacing: 0.01em;
      cursor: pointer;
      box-shadow: 0 4px 14px rgba(31,79,212,0.28);
      transition: transform .06s, box-shadow .15s, opacity .15s;
    }}
    button:hover:not(:disabled) {{ box-shadow: 0 6px 18px rgba(31,79,212,0.36); }}
    button:active:not(:disabled) {{ transform: translateY(1px); }}
    button:disabled {{ opacity: .55; cursor: progress; box-shadow: none; }}
    dl {{
      margin: 0;
      display: grid;
      grid-template-columns: 110px minmax(0, 1fr);
      row-gap: 10px;
      column-gap: 14px;
    }}
    dt {{
      color: var(--muted);
      font-weight: 600;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      align-self: center;
    }}
    dd {{ margin: 0; word-break: break-all; font-weight: 500; }}
    .token-field {{
      display: flex; align-items: stretch; gap: 8px;
    }}
    .token-field code {{
      flex: 1;
      display: block;
      padding: 9px 12px;
      border-radius: 8px;
      border: 1px solid var(--line);
      background: var(--panel-soft);
      font-family: ui-monospace, "JetBrains Mono", "SF Mono", Menlo, monospace;
      font-size: 12.5px;
      overflow-wrap: anywhere;
    }}
    .copy-btn {{
      margin: 0;
      width: auto;
      min-height: 38px;
      padding: 0 12px;
      background: #fff;
      color: var(--ink-soft);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: none;
      font-size: 12px;
      font-weight: 600;
      letter-spacing: 0.02em;
      cursor: pointer;
      transition: background .15s, color .15s, border-color .15s;
    }}
    .copy-btn:hover {{ background: var(--accent-soft); color: var(--accent-deep); border-color: var(--accent); }}
    .copy-btn.copied {{ background: var(--green); color: #fff; border-color: var(--green); }}
    .empty {{
      color: var(--muted);
      padding: 28px 8px;
      text-align: center;
      border: 1px dashed var(--line);
      border-radius: 10px;
      background: var(--panel-soft);
    }}
    .empty .hint {{ display: block; margin-top: 6px; font-size: 12.5px; color: var(--muted); }}
    .quickstart {{ margin-top: 20px; display: grid; gap: 24px; }}
    .client-section {{
      border-top: 1px solid var(--line-soft);
      padding-top: 18px;
    }}
    .client-section:first-child {{ border-top: 0; padding-top: 0; }}
    .client-head {{
      display: flex; align-items: center; gap: 10px;
      margin-bottom: 12px;
    }}
    .client-badge {{
      display: inline-flex; align-items: center;
      padding: 4px 12px;
      background: linear-gradient(135deg, var(--accent), var(--accent-deep));
      color: #fff;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      box-shadow: 0 2px 8px rgba(31,79,212,0.25);
    }}
    .client-badge.codex {{
      background: linear-gradient(135deg, #1d8a6b, #0a5a44);
      box-shadow: 0 2px 8px rgba(10,90,68,0.25);
    }}
    .client-sub {{ color: var(--muted); font-size: 13px; }}
    .steps {{ display: grid; gap: 14px; }}
    .step {{
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #fff;
      overflow: hidden;
    }}
    .step-head {{
      display: flex; align-items: center; gap: 10px;
      padding: 10px 14px;
      border-bottom: 1px solid var(--line-soft);
      background: var(--panel-soft);
    }}
    .step-num {{
      width: 22px; height: 22px;
      border-radius: 50%;
      background: var(--accent);
      color: #fff;
      font-size: 12px;
      font-weight: 700;
      display: inline-flex; align-items: center; justify-content: center;
    }}
    .step-title {{ font-weight: 600; font-size: 13.5px; color: var(--ink-soft); }}
    .step-head .copy-btn {{ margin-left: auto; }}
    pre {{
      margin: 0;
      padding: 14px 16px;
      background: var(--code-bg);
      color: var(--code-ink);
      border: 0;
      font-family: ui-monospace, "JetBrains Mono", "SF Mono", Menlo, monospace;
      font-size: 12.5px;
      line-height: 1.55;
      overflow-x: auto;
      white-space: pre-wrap;
      word-break: break-word;
    }}
    pre .c {{ color: var(--code-comment); }}
    .status {{
      margin-top: 14px;
      min-height: 22px;
      font-weight: 600;
      font-size: 13px;
    }}
    .status.ok {{ color: var(--green); }}
    .status.error {{ color: var(--red); }}
    .footer-note {{
      margin-top: 16px;
      padding: 12px 14px;
      background: var(--accent-soft);
      border-radius: 10px;
      color: var(--accent-deep);
      font-size: 12.5px;
    }}
    .footer-note code {{
      background: rgba(13,43,138,0.10);
      padding: 1px 5px;
      border-radius: 4px;
      font-family: ui-monospace, "JetBrains Mono", "SF Mono", Menlo, monospace;
    }}
    @media (max-width: 880px) {{
      main {{ grid-template-columns: 1fr; }}
      dl {{ grid-template-columns: 1fr; }}
      dt {{ margin-bottom: -4px; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="brand"><span class="dot" aria-hidden="true"></span>Nomos Security Council</div>
    <h1>Agent Signup</h1>
    <p>Mint a Nomos agent account, then wire your CLI (Claude Code or Codex) to route privileged
    shell commands through the council.</p>
  </header>
  <main>
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
          <input id="display-name" name="display_name" autocomplete="name" maxlength="120"
            placeholder="Alice">

          <label for="role">Role</label>
          <select id="role" name="role">__ROLE_OPTIONS__</select>

          <label for="council-id">Council</label>
          <select id="council-id" name="council_id">__COUNCIL_OPTIONS__</select>

          <button id="submit" type="submit">Create Agent</button>
          <div id="status" class="status" aria-live="polite"></div>
        </form>
      </div>
    </section>
    <section>
      <div class="panel-head">
        <h2>Credentials &amp; Client Setup</h2>
        <p class="subtle">Copy these once &mdash; the token is shown one time.</p>
      </div>
      <div class="panel-body">
        <div id="empty" class="empty">
          No agent created yet.
          <span class="hint">Submit the form to mint an account and see install instructions for your client.</span>
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
        </dl>
        <div id="quickstart" class="quickstart" hidden>
          <div class="client-section">
            <div class="client-head">
              <span class="client-badge">Claude Code</span>
              <span class="client-sub">PreToolUse hook for Bash</span>
            </div>
            <div class="steps">
              <div class="step">
                <div class="step-head">
                  <span class="step-num">1</span>
                  <span class="step-title">Clone the plugin repo</span>
                  <button type="button" class="copy-btn" data-copy-target="claude-clone">Copy</button>
                </div>
                <pre id="claude-clone"></pre>
              </div>
              <div class="step">
                <div class="step-head">
                  <span class="step-num">2</span>
                  <span class="step-title">Install &amp; configure council-gate</span>
                  <button type="button" class="copy-btn" data-copy-target="claude-install">Copy</button>
                </div>
                <pre id="claude-install"></pre>
              </div>
              <div class="step">
                <div class="step-head">
                  <span class="step-num">3</span>
                  <span class="step-title">Enable in Claude Code</span>
                  <button type="button" class="copy-btn" data-copy-target="claude-enable">Copy</button>
                </div>
                <pre id="claude-enable"></pre>
              </div>
            </div>
            <div class="footer-note">
              Toggle anytime from any shell: <code>~/.claude/plugins/council-gate/bin/toggle status</code>
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
                  <span class="step-num">1</span>
                  <span class="step-title">Clone the plugin repo</span>
                  <button type="button" class="copy-btn" data-copy-target="codex-clone">Copy</button>
                </div>
                <pre id="codex-clone"></pre>
              </div>
              <div class="step">
                <div class="step-head">
                  <span class="step-num">2</span>
                  <span class="step-title">Write Codex council-gate config</span>
                  <button type="button" class="copy-btn" data-copy-target="codex-config">Copy</button>
                </div>
                <pre id="codex-config"></pre>
              </div>
              <div class="step">
                <div class="step-head">
                  <span class="step-num">3</span>
                  <span class="step-title">Install &amp; enable</span>
                  <button type="button" class="copy-btn" data-copy-target="codex-install">Copy</button>
                </div>
                <pre id="codex-install"></pre>
              </div>
            </div>
            <div class="footer-note">
              Runtime config lives in <code>~/.codex/plugin-state/council-gate/.env</code> (mode 0600).
            </div>
          </div>
        </div>
      </div>
    </section>
  </main>
  <script>
    const form = document.getElementById("signup-form");
    const submit = document.getElementById("submit");
    const statusEl = document.getElementById("status");
    const empty = document.getElementById("empty");
    const result = document.getElementById("result");
    const quickstart = document.getElementById("quickstart");
    const fields = {{
      agent: document.getElementById("out-agent"),
      endpoint: document.getElementById("out-endpoint"),
      token: document.getElementById("out-token"),
      role: document.getElementById("out-role"),
      council: document.getElementById("out-council"),
      claudeClone: document.getElementById("claude-clone"),
      claudeInstall: document.getElementById("claude-install"),
      claudeEnable: document.getElementById("claude-enable"),
      codexClone: document.getElementById("codex-clone"),
      codexConfig: document.getElementById("codex-config"),
      codexInstall: document.getElementById("codex-install"),
    }};

    function shellQuote(value) {{
      return "'" + String(value).replaceAll("'", "'\\\\''") + "'";
    }}

    function renderClaude(data) {{
      fields.claudeClone.textContent = [
        "# Install the council-gate plugin distribution.",
        "# Run the next command from the plugin repository root.",
      ].join("\\n");
      fields.claudeInstall.textContent = [
        "plugins/scripts/install.sh council-gate --non-interactive \\\\",
        `  --backend ${{data.council_endpoint}} \\\\`,
        `  --agent-id ${{data.agent_id}} \\\\`,
        `  --auth-token ${{shellQuote(data.auth_token)}} \\\\`,
        `  --council-id ${{data.council_id}} \\\\`,
        "  --timeout-ms 60000",
      ].join("\\n");
      fields.claudeEnable.textContent = [
        "# In a Claude Code session:",
        "/council-gate on",
        "/council-gate status",
      ].join("\\n");
    }}

    function renderCodex(data) {{
      fields.codexClone.textContent = [
        "# Install the council-gate plugin distribution.",
        "# Run the next command from the plugin repository root.",
      ].join("\\n");
      fields.codexConfig.textContent = [
        "mkdir -p ~/.codex/plugin-state/council-gate",
        "chmod 700 ~/.codex/plugin-state/council-gate",
        "cat > ~/.codex/plugin-state/council-gate/.env <<'EOF'",
        `CODEX_COUNCIL_URL=${{data.council_endpoint}}`,
        `CODEX_COUNCIL_AGENT_ID=${{data.agent_id}}`,
        `CODEX_COUNCIL_AUTH_TOKEN=${{data.auth_token}}`,
        `CODEX_COUNCIL_ID=${{data.council_id}}`,
        "CODEX_COUNCIL_TIMEOUT_MS=60000",
        "EOF",
        "chmod 600 ~/.codex/plugin-state/council-gate/.env",
      ].join("\\n");
      fields.codexInstall.textContent = [
        "plugins/codex-council-gate/scripts/install-local.sh",
        "plugins/codex-council-gate/scripts/toggle on",
      ].join("\\n");
    }}

    function renderQuickstart(data) {{
      renderClaude(data);
      renderCodex(data);
      quickstart.hidden = false;
    }}

    function setupCopyButtons() {{
      document.addEventListener("click", async (event) => {{
        const button = event.target.closest(".copy-btn");
        if (!button) return;
        const targetId = button.getAttribute("data-copy-target");
        const target = targetId && document.getElementById(targetId);
        if (!target) return;
        const text = target.textContent || "";
        try {{
          await navigator.clipboard.writeText(text);
        }} catch (_err) {{
          const range = document.createRange();
          range.selectNodeContents(target);
          const sel = window.getSelection();
          sel.removeAllRanges();
          sel.addRange(range);
          document.execCommand("copy");
          sel.removeAllRanges();
        }}
        const original = button.textContent;
        button.textContent = "Copied";
        button.classList.add("copied");
        setTimeout(() => {{
          button.textContent = original;
          button.classList.remove("copied");
        }}, 1400);
      }});
    }}

    setupCopyButtons();

    form.addEventListener("submit", async (event) => {{
      event.preventDefault();
      submit.disabled = true;
      statusEl.className = "status";
      statusEl.textContent = "";
      const payload = Object.fromEntries(new FormData(form).entries());
      try {{
        const response = await fetch("/api/agents", {{
          method: "POST",
          headers: {{ "content-type": "application/json" }},
          body: JSON.stringify(payload),
        }});
        const data = await response.json();
        if (!response.ok) {{
          throw new Error(data.detail || "request failed");
        }}
        fields.agent.textContent = data.agent_id;
        fields.endpoint.textContent = data.council_endpoint;
        fields.token.textContent = data.auth_token;
        fields.role.textContent = data.role;
        fields.council.textContent = data.council_id;
        renderQuickstart(data);
        empty.hidden = true;
        result.hidden = false;
        statusEl.className = "status ok";
        statusEl.textContent = "Created. Token shown above \\u2014 copy it now.";
      }} catch (error) {{
        statusEl.className = "status error";
        statusEl.textContent = error.message || "request failed";
      }} finally {{
        submit.disabled = false;
      }}
    }});
  </script>
</body>
</html>"""
    return (
        html_doc.replace("{{", "{")
        .replace("}}", "}")
        .replace("__ROLE_OPTIONS__", role_options)
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


app = create_app()
