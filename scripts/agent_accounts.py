#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Manage Nomos agent accounts.

Every mutation delegates to `nomos.auth.provisioning`, the single implementation
shared with the signup console, so the CLI works against whichever durable store
the council reads — Redis or PostgreSQL — selected by --database-url or
COUNCIL_STORAGE_MODE.

Raw tokens are printed only when an account is created or rotated. They cannot be
recovered later because only SHA-256 token hashes are stored.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
import urllib.parse
from collections.abc import AsyncIterator, Iterable, Sequence
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis

from nomos.auth import provisioning

# verify_account_record is used by verify_token. sign_account_record and
# ACCOUNT_INTEGRITY_FIELD are no longer used by this module -- the raw-redis write
# path that called them moved to nomos.auth.provisioning -- but they remain part
# of this module's surface because the account tests construct integrity
# envelopes through it. Re-exported deliberately rather than left as an accident.
from nomos.auth.account_integrity import (  # noqa: F401  (re-exported for tests)
    ACCOUNT_INTEGRITY_FIELD,
    sign_account_record,
    verify_account_record,
)
from nomos.auth.permissions import ALL_PERMISSIONS, ROLE_PERMISSIONS
from nomos.auth.permissions import permissions_for_role as _pkg_permissions_for_role
from nomos.auth.permissions import validate_permissions as _pkg_validate_permissions
from nomos_runtime.redis_client import (
    RedisConnectionConfig,
    create_redis_client,
    parse_sentinel_nodes,
)

# No password in the fallback URL. It used to embed "nomos-dev", which made the
# hardcoded dev password win over the deployment's real COUNCIL_REDIS_PASSWORD:
# the URL password was non-empty, so no env lookup ever happened and every
# invocation against a real deployment failed to authenticate. Keep the dev
# default reachable via DEV_REDIS_PASSWORD below, but let the environment win.
FALLBACK_REDIS_URL = "redis://localhost:6379/0"
DEV_REDIS_PASSWORD = "nomos-dev"  # pragma: allowlist secret
FALLBACK_PREFIX = "nomos:"
FALLBACK_DEFAULT_COUNCIL_ID = "council-prod"
AGENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
COUNCIL_ID_RE = re.compile(r"^[A-Za-z0-9_*.-]+$")
# executor ids follow the build_executor_id convention "{type}:{fp16}", e.g.
# "openshell:2e363bc682be3b5e"; also allow plain ids.
EXECUTOR_ID_RE = re.compile(r"^[A-Za-z0-9_:.-]+$")

AccountRecord = provisioning.AccountRecord


class AccountCliError(Exception):
    """Expected user-facing CLI failure."""


class AccountStoreUnavailableError(AccountCliError):
    """The account store could not be reached, so the answer is unknown.

    Distinguished from a plain ``AccountCliError`` by its exit status so callers
    can tell "this credential is invalid" from "we could not check". Advising an
    operator to rotate a credential because Redis was briefly down would destroy a
    working token.
    """


EXIT_STORE_UNAVAILABLE = 3


def now_timestamp() -> str:
    return str(time.time())


def default_redis_url() -> str:
    """Return the default Redis URL, allowing container wrappers to inject it."""
    return os.environ.get("NOMOS_REDIS_URL") or os.environ.get("REDIS_URL") or FALLBACK_REDIS_URL


def env_first(*keys: str, default: str = "") -> str:
    for key in keys:
        value = os.environ.get(key)
        if value:
            return value
    return default


def env_int(*keys: str, default: int) -> int:
    value = env_first(*keys)
    return int(value) if value else default


def redis_password_from_env() -> str:
    """The data-plane Redis password, resolved as the council resolves it.

    The dev default is last so a real deployment's generated password always wins.
    """
    return env_first("NOMOS_REDIS_PASSWORD", "COUNCIL_REDIS_PASSWORD", default=DEV_REDIS_PASSWORD)


def redis_connection_config(redis_url: str) -> RedisConnectionConfig:
    return RedisConnectionConfig(
        mode=env_first("NOMOS_REDIS_MODE", "COUNCIL_REDIS_MODE", default="standalone"),
        url=redis_url,
        password=_redis_url_parts(redis_url)[3] or redis_password_from_env(),
        sentinel_service_name=env_first(
            "NOMOS_REDIS_SENTINEL_SERVICE_NAME",
            "COUNCIL_REDIS_SENTINEL_SERVICE_NAME",
        ),
        sentinel_nodes=parse_sentinel_nodes(
            env_first("NOMOS_REDIS_SENTINEL_NODES", "COUNCIL_REDIS_SENTINEL_NODES")
        ),
        sentinel_password=env_first(
            "NOMOS_REDIS_SENTINEL_PASSWORD",
            "COUNCIL_REDIS_SENTINEL_PASSWORD",
        ),
        connect_timeout_ms=env_int(
            "NOMOS_REDIS_CONNECT_TIMEOUT_MS",
            "COUNCIL_REDIS_CONNECT_TIMEOUT_MS",
            default=1000,
        ),
        command_timeout_ms=env_int(
            "NOMOS_REDIS_COMMAND_TIMEOUT_MS",
            "COUNCIL_REDIS_COMMAND_TIMEOUT_MS",
            default=1000,
        ),
    )


def default_prefix() -> str:
    """Return the default Redis key prefix namespace."""
    return os.environ.get("NOMOS_REDIS_PREFIX") or FALLBACK_PREFIX


def default_council_id() -> str:
    """Return the default council membership granted during account creation."""
    return (
        os.environ.get("NOMOS_DEFAULT_COUNCIL_ID")
        or os.environ.get("COUNCIL_DEFAULT_COUNCIL_ID")
        or FALLBACK_DEFAULT_COUNCIL_ID
    )


def default_account_integrity_key() -> str:
    return os.environ.get("NOMOS_ACCOUNT_INTEGRITY_KEY") or os.environ.get(
        "COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY",
        "",
    )


def validate_agent_id(agent_id: str) -> str:
    if not AGENT_ID_RE.fullmatch(agent_id):
        raise AccountCliError("agent_id must contain only letters, numbers, hyphen, or underscore")
    return agent_id


def validate_council_id(council_id: str) -> str:
    if not COUNCIL_ID_RE.fullmatch(council_id):
        raise AccountCliError(
            "council_id must contain only letters, numbers, hyphen, underscore, dot, or '*'"
        )
    return council_id


def validate_permissions(permissions: Iterable[str]) -> tuple[str, ...]:
    """Package validation, surfaced as the CLI's error type."""
    try:
        return _pkg_validate_permissions(permissions)
    except ValueError as exc:
        raise AccountCliError(str(exc)) from exc


def permissions_for_role(role: str) -> tuple[str, ...]:
    """Package role lookup, surfaced as the CLI's error type."""
    try:
        return _pkg_permissions_for_role(role)
    except ValueError as exc:
        raise AccountCliError(str(exc)) from exc


def resolve_permissions(
    role: str | None,
    explicit: Sequence[str],
    *,
    default_role: str | None = "developer",
) -> tuple[str, ...]:
    """Resolve --role / --permission into scopes.

    The flag-shape rules are CLI concerns and keep CLI wording; the role and
    permission tables themselves come from nomos.auth.permissions.
    """
    if role and explicit:
        raise AccountCliError("use either --role or --permission, not both")
    if explicit:
        return validate_permissions(explicit)
    if role or default_role:
        return validate_permissions(permissions_for_role(role or default_role or ""))
    raise AccountCliError("provide --role or at least one --permission")


# Redis key layout. This branch routes account *writes* through
# nomos.auth.provisioning and deleted these builders along with the raw-redis
# write path, but upstream's verify-token reads the same keys directly and needs
# them. Reads are a different concern from the write-ordering these functions
# used to serve, so they live on rather than being reintroduced into provisioning.
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


def generate_token() -> str:
    return secrets.token_hex(32)


def token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


async def connect(redis_url: str) -> aioredis.Redis:
    try:
        config = redis_connection_config(redis_url)
        client = create_redis_client(config)
    except ValueError as exc:
        raise AccountCliError(str(exc)) from exc
    try:
        await client.ping()
    except Exception as exc:  # pragma: no cover - depends on local Redis
        await client.aclose()
        raise AccountCliError(f"cannot connect to Redis in {config.mode} mode: {exc}") from exc
    return client


def redact_secrets(exc: Exception, *extra: str) -> str:
    """Render a driver exception without the credentials it may have quoted.

    psycopg and redis-py both echo the connection string on failure, and those
    URLs embed passwords. These messages now reach operators: the compose
    register wrapper prints stderr, and deploy_stack.sh captures it, so an
    unredacted driver error lands in deploy and CI logs. Host and database name
    survive, which is what makes the message actionable.
    """
    text = str(exc)
    secrets = {
        p
        for p in (_url_password(v) for v in (*extra, os.environ.get("COUNCIL_DATABASE_URL", "")))
        if p
    }
    secrets |= {p for p in (redis_password_from_env(), os.environ.get("REDIS_PASSWORD", "")) if p}
    for secret in secrets:
        text = text.replace(secret, "***")
    # Belt and braces: collapse any surviving inline URL credential pair, so a
    # password we did not know to look for still never reaches the operator.
    pattern = r"(?P<scheme>\w+://)[^:/@\s]+:[^@\s]+@"  # pragma: allowlist secret
    return re.sub(pattern, r"\g<scheme>***:***@", text)


def _url_password(url: str) -> str:
    try:
        return urllib.parse.unquote(urllib.parse.urlparse(url or "").password or "")
    except ValueError:
        return ""


async def create_account(args: argparse.Namespace) -> dict[str, Any]:
    validate_agent_id(args.agent_id)
    # --default-council is the fallback when no --council is given. Reading
    # default_council_id() directly here made the flag silently inert.
    fallback = validate_council_id(getattr(args, "default_council", "") or default_council_id())
    councils = [validate_council_id(c) for c in (args.council or [])] or [fallback]
    async with open_account_store(args) as store:
        return await provisioning.create_account(
            store,
            agent_id=args.agent_id,
            role=args.role,
            display_name=args.display_name or args.agent_id,
            councils=councils,
            permissions=args.permission or (),
            force=args.force,
            # --disabled was parsed and then dropped, so an operator asking for an
            # inert account received an authenticating one holding a live token.
            enabled=not getattr(args, "disabled", False),
        )


async def list_accounts(args: argparse.Namespace) -> dict[str, Any]:
    async with open_account_store(args) as store:
        records = await provisioning.list_accounts(store)
    return {"accounts": [record.as_dict() for record in records]}


async def show_account(args: argparse.Namespace) -> dict[str, Any]:
    validate_agent_id(args.agent_id)
    async with open_account_store(args) as store:
        record = await provisioning.load_record(store, args.agent_id)
    if record is None:
        raise AccountCliError(f"account {args.agent_id!r} does not exist")
    return record.as_dict()


async def verify_token(args: argparse.Namespace) -> dict[str, Any]:
    """Verify a one-way token candidate without returning either token or hash.

    Mirrors the checks ``AgentAuthenticator.authenticate`` performs, so a token
    accepted here is one live MCP authentication also accepts: reverse index,
    stored hash, enabled flag, and the integrity envelope over the account record
    together with its permissions and councils. Without the envelope check a
    deployment can report a healthy credential that the council then rejects for a
    stale or forged envelope.

    ``--expect-council`` additionally requires stored membership, so reuse of an
    existing account cannot be reported as healthy when it belongs to a different
    council than the deployment is configured for.
    """
    agent_id = validate_agent_id(args.agent_id)
    raw_token = sys.stdin.read().strip() if args.token_stdin else ""
    if not raw_token:
        raise AccountCliError("token candidate is empty")

    # Accept both repeated flags and the comma-separated form AGENT_COUNCILS uses,
    # including whitespace around the separators.
    expected_councils = {
        validate_council_id(part.strip())
        for value in (args.expect_council or [])
        for part in value.split(",")
        if part.strip()
    }

    candidate_hash = token_hash(raw_token)
    try:
        client = await connect(args.redis_url)
    except Exception as exc:  # unreachable store: unknown, not invalid
        raise AccountStoreUnavailableError(f"cannot reach the account store: {exc}") from exc
    try:
        try:
            account = await client.hgetall(account_key(args.prefix, agent_id))
            indexed_agent_id = await client.get(token_index_key(args.prefix, candidate_hash))
            permissions = set(await client.smembers(perms_key(args.prefix, agent_id)))
            councils = set(await client.smembers(account_councils_key(args.prefix, agent_id)))
        except Exception as exc:
            raise AccountStoreUnavailableError(f"cannot read the account store: {exc}") from exc

        stored_hash = account.get("token_hash", "") if account else ""
        hash_matches = hmac.compare_digest(stored_hash, candidate_hash)
        verified = account.get("enabled") == "1" and indexed_agent_id == agent_id and hash_matches
        if not verified:
            raise AccountCliError(f"token does not authenticate account {agent_id!r}")

        integrity_checked = bool(args.account_integrity_key)
        if integrity_checked and not verify_account_record(
            key=args.account_integrity_key,
            agent_id=agent_id,
            account=account,
            permissions=permissions,
            councils=councils,
        ):
            raise AccountCliError(
                f"account {agent_id!r} has an invalid account-integrity envelope; "
                "live authentication rejects it"
            )

        if expected_councils and "*" not in councils:
            missing = expected_councils - councils
            if missing:
                raise AccountCliError(
                    f"account {agent_id!r} is not a member of council(s): "
                    f"{', '.join(sorted(missing))}"
                )

        # Reported so a caller can tell full verification from the weaker check
        # performed when no integrity key is configured; without it an unsigned
        # envelope would be indistinguishable from a verified one.
        return {
            "agent_id": agent_id,
            "verified": True,
            "integrity_checked": integrity_checked,
        }
    finally:
        await client.aclose()


async def rotate_token(args: argparse.Namespace) -> dict[str, Any]:
    validate_agent_id(args.agent_id)
    async with open_account_store(args) as store:
        return await provisioning.rotate_token(store, agent_id=args.agent_id)


async def set_enabled(args: argparse.Namespace, enabled: bool) -> dict[str, Any]:
    validate_agent_id(args.agent_id)
    async with open_account_store(args) as store:
        return await provisioning.set_enabled(store, agent_id=args.agent_id, enabled=enabled)


async def set_permissions(args: argparse.Namespace) -> dict[str, Any]:
    validate_agent_id(args.agent_id)
    async with open_account_store(args) as store:
        return await provisioning.set_permissions(
            store,
            agent_id=args.agent_id,
            role=args.role,
            permissions=args.permission or (),
        )


async def delete_account(args: argparse.Namespace) -> dict[str, Any]:
    validate_agent_id(args.agent_id)
    if not args.yes:
        raise AccountCliError("refusing to delete without --yes")
    async with open_account_store(args) as store:
        return await provisioning.delete_account(store, agent_id=args.agent_id)


async def grant_council(args: argparse.Namespace) -> dict[str, Any]:
    validate_agent_id(args.agent_id)
    council_id = validate_council_id(args.council_id)
    async with open_account_store(args) as store:
        return await provisioning.grant_council(
            store, agent_id=args.agent_id, council_id=council_id
        )


async def revoke_council(args: argparse.Namespace) -> dict[str, Any]:
    validate_agent_id(args.agent_id)
    council_id = validate_council_id(args.council_id)
    async with open_account_store(args) as store:
        return await provisioning.revoke_council(
            store, agent_id=args.agent_id, council_id=council_id
        )


async def account_councils(args: argparse.Namespace) -> dict[str, Any]:
    validate_agent_id(args.agent_id)
    async with open_account_store(args) as store:
        return await provisioning.account_councils(store, agent_id=args.agent_id)


async def council_members(args: argparse.Namespace) -> dict[str, Any]:
    council_id = validate_council_id(args.council_id)
    async with open_account_store(args) as store:
        return await provisioning.council_members(store, council_id=council_id)


def print_text(command: str, result: dict[str, Any]) -> None:
    if command == "verify-token":
        print(f"Verified token for account: {result['agent_id']}")
        return

    if command == "list":
        accounts = result["accounts"]
        if not accounts:
            print("No accounts found.")
            return
        for account in accounts:
            status = "enabled" if account["enabled"] else "disabled"
            role = account.get("role") or "custom"
            print(f"{account['agent_id']}\t{status}\t{role}\t{account['display_name']}")
        return

    if command == "delete":
        print(f"Deleted account: {result['agent_id']}")
        return

    if command == "councils":
        print(f"Agent ID:        {result['agent_id']}")
        print(f"Councils:        {', '.join(result['councils']) or '(none)'}")
        return

    if command == "members":
        print(f"Council ID:      {result['council_id']}")
        print(f"Agents:          {', '.join(result['agents']) or '(none)'}")
        return

    if command == "trusted-executor":
        if "trusted_executors" in result:
            tes = result["trusted_executors"]
            if not tes:
                print("No trusted executors found.")
                return
            for te in tes:
                status = "enabled" if te.get("enabled") == "1" else "disabled"
                print(f"{te['key_fingerprint']}\t{te['executor_id']}\t{status}")
            return
        for te in result.get("registered", []):
            status = "enabled" if te.get("enabled") == "1" else "disabled"
            print(f"Registered: {te['executor_id']}  fp={te['key_fingerprint']}  {status}")
        print(f"count={result.get('count', 0)}")
        return

    token = result.get("token")
    print(f"Agent ID:        {result['agent_id']}")
    print(f"Display Name:    {result['display_name']}")
    print(f"Enabled:         {str(result['enabled']).lower()}")
    print(f"Role:            {result.get('role') or 'custom'}")
    print(f"Permissions:     {', '.join(result['permissions']) or '(none)'}")
    print(f"Councils:        {', '.join(result['councils']) or '(none)'}")
    print(f"Token Hash:      {result['token_hash_prefix']}")
    if token:
        print(f"Token:           {token}")
        print("Store this token securely. It cannot be recovered from Redis.")


def add_database_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--database-url",
        default=os.environ.get("COUNCIL_DATABASE_URL") or os.environ.get("DATABASE_URL") or "",
        help="Durable store DSN. Set (or COUNCIL_STORAGE_MODE=postgres) to target "
        "PostgreSQL — the store the council reads in service mode. Empty → Redis.",
    )


def add_connection_args(parser: argparse.ArgumentParser) -> None:
    redis_url = default_redis_url()
    prefix = default_prefix()
    integrity_key = default_account_integrity_key()
    parser.add_argument(
        "--redis-url",
        default=redis_url,
        help=f"Redis connection URL (default: {redis_url})",
    )
    parser.add_argument(
        "--prefix",
        default=prefix,
        help=f"Redis key prefix namespace (default: {prefix})",
    )
    parser.add_argument(
        "--account-integrity-key",
        default=integrity_key,
        help="HMAC key for account-critical Redis state",
    )
    add_database_arg(parser)
    parser.add_argument(
        "--allow-unsigned-accounts",
        action="store_true",
        help="Provision even when no account integrity key is configured. Such "
        "accounts are rejected by a council that has one; postgres refuses without this.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")


def add_permission_args(parser: argparse.ArgumentParser, *, default_role: str | None) -> None:
    parser.add_argument(
        "--role",
        choices=sorted(ROLE_PERMISSIONS),
        default=default_role,
        help="Permission preset",
    )
    parser.add_argument(
        "--permission",
        action="append",
        default=[],
        choices=ALL_PERMISSIONS,
        help="Explicit permission scope; may be repeated",
    )


# ── trusted executors ──────────────────────────────────────────────────────
# A trusted executor is an executor evidence-signing key the council trusts when
# validating certified evidence. The council looks it up in its DURABLE store
# (PostgreSQL in service mode), so registration must target that store — not the
# redis the executor self-registers into. Both stores expose the same
# register/get/list_trusted_executor surface (nomos.store.base.DurableStore).


def validate_executor_id(executor_id: str) -> str:
    if not executor_id or not EXECUTOR_ID_RE.fullmatch(executor_id):
        raise AccountCliError("executor_id must be non-empty (letters, digits, and _-:. only)")
    return executor_id


def fingerprint_from_pubkey_hex(public_key_hex: str) -> tuple[str, str]:
    """Return (normalized_hex, key_fingerprint). Fingerprint = sha256(raw 32B key),
    matching the council's recompute in nomos/nat/functions.py and the executor's
    ExecutorIdentity.key_fingerprint."""
    cleaned = public_key_hex.strip().lower()
    try:
        raw = bytes.fromhex(cleaned)
    except ValueError as exc:
        raise AccountCliError(f"public key is not valid hex: {exc}") from exc
    if len(raw) != 32:
        raise AccountCliError(f"public key must decode to 32 bytes (ed25519); got {len(raw)}")
    return cleaned, hashlib.sha256(raw).hexdigest()


def resolve_pubkey_hex(args: argparse.Namespace) -> str:
    if getattr(args, "public_key_hex", ""):
        return args.public_key_hex
    if getattr(args, "public_key_file", ""):
        raw = Path(args.public_key_file).read_bytes()
        return raw.hex() if len(raw) == 32 else raw.decode().strip()
    raise AccountCliError("provide --public-key-hex, --public-key-file, or --from-redis")


class _RedisTrustedExecutors:
    """Raw-redis target mirroring RedisStore's trusted_executor key layout, so the
    redis path needs no RedisConfig construction and stays byte-identical to what
    the executor/RedisStore write."""

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
        now = now_timestamp()
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

    async def list_trusted_executors(self) -> list[dict[str, str]]:
        out = []
        for fp in sorted(await self._c.smembers(self._index)):
            rec = await self.get_trusted_executor(fp)
            if rec:
                out.append(rec)
        return out


def storage_mode(args: argparse.Namespace) -> str:
    """Return "postgres" or "redis" — the single backend decision for the CLI."""
    if getattr(args, "database_url", "") or "":
        return "postgres"
    if os.environ.get("COUNCIL_STORAGE_MODE", "").strip().lower() == "postgres":
        return "postgres"
    return "redis"


def _redis_url_parts(url: str) -> tuple[str, int, int, str, bool]:
    """Decompose a redis:// URL into the fields RedisConfig actually has."""
    parsed = urllib.parse.urlparse(url or "")
    if parsed.scheme not in {"", "redis", "rediss"}:
        raise AccountCliError(
            f"unsupported Redis URL scheme {parsed.scheme!r} (expected redis:// or rediss://)"
        )
    try:
        db = int((parsed.path or "").lstrip("/") or 0)
    except ValueError as exc:
        raise AccountCliError(f"invalid Redis database in URL: {parsed.path!r}") from exc
    return (
        parsed.hostname or "localhost",
        parsed.port or 6379,
        db,
        urllib.parse.unquote(parsed.password or ""),
        parsed.scheme == "rediss",
    )


def redis_config_from_args(args: argparse.Namespace) -> Any:
    """Build the council's RedisConfig from --redis-url plus the env the council
    reads. RedisConfig has no url field, so the URL is decomposed here.

    Sentinel note: RedisStore.connect passes config.password and never a URL, so
    the data-plane password must be lifted off the URL or sentinel deployments
    would authenticate with nothing.

    The password may legitimately arrive either way. deploy_stack.sh embeds it in
    --redis-url, but the council image supplies it as COUNCIL_REDIS_PASSWORD with
    no URL at all -- which is how the compose-* wrappers invoke this CLI. Resolve
    it the way the council does: an explicit URL password wins, else the
    environment, else the dev default.
    """
    from nomos.config.settings import RedisConfig

    host, port, db, password, tls = _redis_url_parts(args.redis_url)
    password = password or redis_password_from_env()
    try:
        return RedisConfig(
            mode=os.environ.get("COUNCIL_REDIS_MODE", "standalone").strip() or "standalone",
            host=host,
            port=port,
            db=db,
            password=password,
            tls=tls,
            key_prefix=args.prefix,
            account_integrity_key=args.account_integrity_key,
            sentinel_service_name=os.environ.get("COUNCIL_REDIS_SENTINEL_SERVICE_NAME", ""),
            sentinel_nodes=os.environ.get("COUNCIL_REDIS_SENTINEL_NODES", ""),
            sentinel_password=os.environ.get("COUNCIL_REDIS_SENTINEL_PASSWORD", ""),
        )
    except AccountCliError:
        raise
    except Exception as exc:  # pydantic ValidationError → user-facing message
        raise AccountCliError(f"invalid Redis configuration: {exc}") from exc


@contextlib.asynccontextmanager
async def open_durable_store(args: argparse.Namespace) -> AsyncIterator[Any]:
    """Yield the store the council reads — PostgresStore or RedisStore.

    Both branches carry the account integrity key. Without it the store writes no
    `_integrity_hmac`, `verify_account_integrity` passes vacuously, and the
    council rejects at login every account written through here.
    """
    if storage_mode(args) == "postgres":
        db = getattr(args, "database_url", "") or ""
        if not db:
            raise AccountCliError("postgres mode requires --database-url (or COUNCIL_DATABASE_URL)")
        from nomos.store.postgres_store import PostgresStore

        store = PostgresStore(db, account_integrity_key=args.account_integrity_key)
        try:
            await store.connect()
        except Exception as exc:  # pragma: no cover - depends on a live PG
            raise AccountCliError(
                f"cannot connect to PostgreSQL: {redact_secrets(exc, db)}"
            ) from exc
        try:
            yield store
        finally:
            await store.close()
    else:
        from nomos.store.redis_store import RedisStore

        redis_store = RedisStore(redis_config_from_args(args))
        try:
            await redis_store.connect()
        except Exception as exc:  # pragma: no cover - depends on local Redis
            raise AccountCliError(f"cannot connect to Redis: {redact_secrets(exc)}") from exc
        try:
            yield redis_store
        finally:
            await redis_store.close()


@contextlib.asynccontextmanager
async def open_account_store(args: argparse.Namespace) -> AsyncIterator[Any]:
    """open_durable_store plus the integrity-key precondition that account writes
    depend on (trusted-executor writes do not)."""
    async with open_durable_store(args) as store:
        warning = provisioning.require_signed_store(
            store,
            storage_mode=storage_mode(args),
            allow_unsigned=getattr(args, "allow_unsigned_accounts", False),
        )
        if warning:
            print(f"warning: {warning}", file=sys.stderr)
        yield store


async def register_trusted_executor_record(
    store: Any,
    *,
    executor_id: str,
    public_key_hex: str,
    enabled: bool,
    metadata: dict[str, Any] | None,
) -> dict[str, str]:
    """Validate + register one trusted executor; return the read-back record."""
    validate_executor_id(executor_id)
    cleaned_hex, key_fingerprint = fingerprint_from_pubkey_hex(public_key_hex)
    await store.register_trusted_executor(
        executor_id=executor_id,
        public_key_hex=cleaned_hex,
        key_fingerprint=key_fingerprint,
        metadata=metadata or {},
        enabled=enabled,
    )
    record = await store.get_trusted_executor(key_fingerprint)
    if not record:
        raise AccountCliError(f"register succeeded but read-back failed for {key_fingerprint}")
    return record


async def trusted_executor_register(args: argparse.Namespace) -> dict[str, Any]:
    try:
        metadata = json.loads(args.metadata_json)
    except json.JSONDecodeError as exc:
        raise AccountCliError(f"--metadata-json is not valid JSON: {exc}") from exc

    records: list[dict[str, str]] = []
    if args.from_redis:
        # Source = the redis the executor self-registered into; target = durable store.
        source = await connect(args.redis_url)
        try:
            fps = {str(fp) for fp in await source.smembers(f"{args.prefix}trusted_executors")}
            if args.fingerprint:
                fps &= {args.fingerprint}
            if not fps:
                raise AccountCliError("no matching trusted executors found in redis")
            async with open_durable_store(args) as target:
                for fp in sorted(fps):
                    rec = await source.hgetall(f"{args.prefix}trusted_executors:{fp}")
                    if not rec:
                        continue
                    if args.executor_id and rec.get("executor_id") != args.executor_id:
                        continue
                    try:
                        rec_meta = json.loads(rec.get("metadata_json", "{}"))
                    except json.JSONDecodeError:
                        rec_meta = {}
                    records.append(
                        await register_trusted_executor_record(
                            target,
                            executor_id=rec["executor_id"],
                            public_key_hex=rec["public_key_hex"],
                            enabled=rec.get("enabled", "1") == "1",
                            metadata=rec_meta,
                        )
                    )
        finally:
            await source.aclose()
        if not records:
            raise AccountCliError("no trusted executors matched the given filters")
    else:
        if not args.executor_id:
            raise AccountCliError("executor_id is required unless --from-redis is used")
        public_key_hex = resolve_pubkey_hex(args)
        async with open_durable_store(args) as target:
            records.append(
                await register_trusted_executor_record(
                    target,
                    executor_id=args.executor_id,
                    public_key_hex=public_key_hex,
                    enabled=not args.disabled,
                    metadata=metadata,
                )
            )
    return {"registered": records, "count": len(records)}


async def trusted_executor_list(args: argparse.Namespace) -> dict[str, Any]:
    async with open_durable_store(args) as store:
        return {"trusted_executors": await store.list_trusted_executors()}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage Nomos agent accounts")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="Create an account and print its raw token")
    add_connection_args(create)
    add_permission_args(create, default_role=None)
    create.add_argument("agent_id")
    create.add_argument("--display-name", default="")
    create.add_argument(
        "--council",
        action="append",
        default=[],
        help=(
            "Council id this account may petition; may be repeated. Omit to grant "
            "the default council membership."
        ),
    )
    create.add_argument(
        "--default-council",
        default=default_council_id(),
        help=(
            "Default council granted when --council is omitted "
            f"(default: {FALLBACK_DEFAULT_COUNCIL_ID}, or NOMOS_DEFAULT_COUNCIL_ID)"
        ),
    )
    create.add_argument("--disabled", action="store_true", help="Create account disabled")
    create.add_argument("--force", action="store_true", help="Replace an existing account")
    create.set_defaults(handler=create_account)

    list_parser = subparsers.add_parser("list", help="List accounts")
    add_connection_args(list_parser)
    list_parser.set_defaults(handler=list_accounts)

    show = subparsers.add_parser("show", help="Show one account")
    add_connection_args(show)
    show.add_argument("agent_id")
    show.set_defaults(handler=show_account)

    verify = subparsers.add_parser(
        "verify-token",
        help="Verify a raw token from standard input without printing it or its hash",
    )
    add_connection_args(verify)
    verify.add_argument("--token-stdin", action="store_true", required=True)
    verify.add_argument(
        "--expect-council",
        action="append",
        help=(
            "Require the account to be a member of this council; repeatable. "
            "Verification fails if stored membership does not include it."
        ),
    )
    verify.add_argument("agent_id")
    verify.set_defaults(handler=verify_token)

    rotate = subparsers.add_parser("rotate-token", help="Rotate an account token")
    add_connection_args(rotate)
    rotate.add_argument("agent_id")
    rotate.set_defaults(handler=rotate_token)

    enable = subparsers.add_parser("enable", help="Enable an account")
    add_connection_args(enable)
    enable.add_argument("agent_id")
    enable.set_defaults(handler=lambda args: set_enabled(args, True))

    disable = subparsers.add_parser("disable", help="Disable an account")
    add_connection_args(disable)
    disable.add_argument("agent_id")
    disable.set_defaults(handler=lambda args: set_enabled(args, False))

    perms = subparsers.add_parser("set-permissions", help="Replace account permissions")
    add_connection_args(perms)
    add_permission_args(perms, default_role=None)
    perms.add_argument("agent_id")
    perms.set_defaults(handler=set_permissions)

    grant = subparsers.add_parser("grant-council", help="Allow an account to petition a council")
    add_connection_args(grant)
    grant.add_argument("agent_id")
    grant.add_argument("council_id")
    grant.set_defaults(handler=grant_council)

    revoke = subparsers.add_parser(
        "revoke-council",
        help="Remove an account's ability to petition a council",
    )
    add_connection_args(revoke)
    revoke.add_argument("agent_id")
    revoke.add_argument("council_id")
    revoke.set_defaults(handler=revoke_council)

    councils = subparsers.add_parser("councils", help="List councils an account can petition")
    add_connection_args(councils)
    councils.add_argument("agent_id")
    councils.set_defaults(handler=account_councils)

    members = subparsers.add_parser("members", help="List agents allowed to petition a council")
    add_connection_args(members)
    members.add_argument("council_id")
    members.set_defaults(handler=council_members)

    delete = subparsers.add_parser("delete", help="Delete an account")
    add_connection_args(delete)
    delete.add_argument("agent_id")
    delete.add_argument("--yes", action="store_true", help="Confirm account deletion")
    delete.set_defaults(handler=delete_account)

    te = subparsers.add_parser(
        "trusted-executor",
        help="Manage trusted executors (certified-evidence signing keys)",
    )
    te_sub = te.add_subparsers(dest="te_action", required=True)

    te_reg = te_sub.add_parser("register", help="Register a trusted executor key")
    add_connection_args(te_reg)
    te_reg.add_argument(
        "executor_id",
        nargs="?",
        help="Executor id (e.g. openshell:2e363bc682be3b5e). "
        "Required unless --from-redis; with --from-redis, an optional filter.",
    )
    src = te_reg.add_mutually_exclusive_group()
    src.add_argument("--public-key-hex", default="", help="64-char hex ed25519 public key")
    src.add_argument(
        "--public-key-file", default="", help="File with the raw 32B or hex public key"
    )
    src.add_argument(
        "--from-redis",
        action="store_true",
        help="Sync executor self-registrations from redis into the durable store",
    )
    te_reg.add_argument("--fingerprint", default="", help="(--from-redis) only this fingerprint")
    te_reg.add_argument("--metadata-json", default="{}", help="Optional JSON metadata")
    te_reg.add_argument("--disabled", action="store_true", help="Register as disabled")
    te_reg.set_defaults(handler=trusted_executor_register)

    te_list = te_sub.add_parser("list", help="List registered trusted executors")
    add_connection_args(te_list)
    te_list.set_defaults(handler=trusted_executor_list)

    return parser


async def run(args: argparse.Namespace) -> int:
    try:
        result = await args.handler(args)
    except AccountStoreUnavailableError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_STORE_UNAVAILABLE
    except AccountCliError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print_text(args.command, result)
    return 0


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
