#!/usr/bin/env python3
"""
Manage Nomos agent accounts in Redis.

The CLI uses the same account schema as AgentAuthenticator:

    nomos:accounts:{agent_id}        hash: token_hash, display_name, created_at, enabled
    nomos:accounts:{agent_id}:perms  set: permission strings
    nomos:accounts:{agent_id}:councils set: council_ids this agent may petition
    nomos:councils:{council_id}:agents set: agent_ids allowed to petition this council
    nomos:token_index:{token_hash}   string: agent_id

Raw tokens are printed only when an account is created or rotated. They cannot
be recovered later because Redis stores only SHA-256 token hashes.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import redis.asyncio as aioredis

from nomos.auth.account_integrity import ACCOUNT_INTEGRITY_FIELD, sign_account_record
from nomos.auth.permissions import PermissionScope
from nomos_runtime.redis_client import (
    RedisConnectionConfig,
    create_redis_client,
    parse_sentinel_nodes,
)

FALLBACK_REDIS_URL = "redis://:nomos-dev@localhost:6379/0"
FALLBACK_PREFIX = "nomos:"
FALLBACK_DEFAULT_COUNCIL_ID = "council-prod"
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
    "demo": (
        PermissionScope.SHELL_READ.value,
        PermissionScope.SHELL_WRITE.value,
        PermissionScope.FILE_READ.value,
        PermissionScope.FILE_WRITE.value,
    ),
}


@dataclass(frozen=True)
class AccountRecord:
    agent_id: str
    display_name: str
    enabled: bool
    permissions: tuple[str, ...]
    councils: tuple[str, ...]
    role: str
    created_at: str
    updated_at: str
    token_hash_prefix: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "display_name": self.display_name,
            "enabled": self.enabled,
            "permissions": list(self.permissions),
            "councils": list(self.councils),
            "role": self.role,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "token_hash_prefix": self.token_hash_prefix,
        }


class AccountCliError(Exception):
    """Expected user-facing CLI failure."""


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


def redis_connection_config(redis_url: str) -> RedisConnectionConfig:
    return RedisConnectionConfig(
        mode=env_first("NOMOS_REDIS_MODE", "COUNCIL_REDIS_MODE", default="standalone"),
        url=redis_url,
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
    known = set(ALL_PERMISSIONS)
    invalid = sorted(set(permissions) - known)
    if invalid:
        raise AccountCliError(
            "unknown permission(s): "
            + ", ".join(invalid)
            + ". Known permissions: "
            + ", ".join(ALL_PERMISSIONS)
        )
    return tuple(sorted(set(permissions)))


def permissions_for_role(role: str) -> tuple[str, ...]:
    try:
        return ROLE_PERMISSIONS[role]
    except KeyError as exc:
        raise AccountCliError(
            f"unknown role {role!r}. Known roles: {', '.join(sorted(ROLE_PERMISSIONS))}"
        ) from exc


def resolve_permissions(
    role: str | None,
    explicit: Sequence[str],
    *,
    default_role: str | None = "developer",
) -> tuple[str, ...]:
    if role and explicit:
        raise AccountCliError("use either --role or --permission, not both")
    if explicit:
        return validate_permissions(explicit)
    if role or default_role:
        return validate_permissions(permissions_for_role(role or default_role or ""))
    raise AccountCliError("provide --role or at least one --permission")


def generate_token() -> str:
    return secrets.token_hex(32)


def token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


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


def build_account_mapping(
    *,
    token_hash_value: str,
    agent_id: str,
    display_name: str,
    enabled: bool,
    role: str,
    created_at: str,
    updated_at: str,
) -> dict[str, str]:
    return {
        "token_hash": token_hash_value,
        "display_name": display_name or agent_id,
        "created_at": created_at,
        "updated_at": updated_at,
        "enabled": "1" if enabled else "0",
        "role": role,
    }


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


async def load_record(client: aioredis.Redis, prefix: str, agent_id: str) -> AccountRecord | None:
    account = await client.hgetall(account_key(prefix, agent_id))
    if not account:
        return None
    permissions = sorted(await client.smembers(perms_key(prefix, agent_id)))
    councils = sorted(await client.smembers(account_councils_key(prefix, agent_id)))
    stored_hash = account.get("token_hash", "")
    return AccountRecord(
        agent_id=agent_id,
        display_name=account.get("display_name", agent_id),
        enabled=account.get("enabled") == "1",
        permissions=tuple(permissions),
        councils=tuple(councils),
        role=account.get("role", ""),
        created_at=account.get("created_at", ""),
        updated_at=account.get("updated_at", ""),
        token_hash_prefix=f"{stored_hash[:16]}..." if stored_hash else "",
    )


async def refresh_account_integrity(
    client: aioredis.Redis,
    prefix: str,
    agent_id: str,
    key: str,
) -> None:
    if not key:
        return
    account = await client.hgetall(account_key(prefix, agent_id))
    if not account:
        return
    permissions = set(await client.smembers(perms_key(prefix, agent_id)))
    councils = set(await client.smembers(account_councils_key(prefix, agent_id)))
    signature = sign_account_record(
        key=key,
        agent_id=agent_id,
        account=account,
        permissions=permissions,
        councils=councils,
    )
    await client.hset(
        account_key(prefix, agent_id),
        mapping={ACCOUNT_INTEGRITY_FIELD: signature},
    )


async def create_account(args: argparse.Namespace) -> dict[str, Any]:
    agent_id = validate_agent_id(args.agent_id)
    requested_councils = args.council or [args.default_council]
    councils = tuple(validate_council_id(c) for c in requested_councils)
    permissions = resolve_permissions(args.role, args.permission, default_role="developer")
    role = args.role or ("custom" if args.permission else "developer")
    display_name = args.display_name or agent_id
    raw_token = generate_token()
    hashed_token = token_hash(raw_token)
    client = await connect(args.redis_url)
    try:
        existing = await client.hgetall(account_key(args.prefix, agent_id))
        if existing and not args.force:
            raise AccountCliError(f"account {agent_id!r} already exists; use --force to replace it")

        ts = now_timestamp()
        old_hash = existing.get("token_hash") if existing else None
        old_councils = await client.smembers(account_councils_key(args.prefix, agent_id))
        created_at = existing.get("created_at", ts) if existing else ts
        pipe = client.pipeline()
        pipe.hset(
            account_key(args.prefix, agent_id),
            mapping=build_account_mapping(
                token_hash_value=hashed_token,
                agent_id=agent_id,
                display_name=display_name,
                enabled=not args.disabled,
                role=role,
                created_at=created_at,
                updated_at=ts,
            ),
        )
        pipe.delete(perms_key(args.prefix, agent_id))
        if permissions:
            pipe.sadd(perms_key(args.prefix, agent_id), *permissions)
        pipe.delete(account_councils_key(args.prefix, agent_id))
        for council_id in old_councils - set(councils):
            pipe.srem(council_agents_key(args.prefix, council_id), agent_id)
        for council_id in councils:
            pipe.sadd(account_councils_key(args.prefix, agent_id), council_id)
            pipe.sadd(council_agents_key(args.prefix, council_id), agent_id)
        if old_hash:
            pipe.delete(token_index_key(args.prefix, old_hash))
        pipe.set(token_index_key(args.prefix, hashed_token), agent_id)
        await pipe.execute()
        await refresh_account_integrity(
            client,
            args.prefix,
            agent_id,
            args.account_integrity_key,
        )

        record = await load_record(client, args.prefix, agent_id)
        result = record.as_dict() if record else {}
        result["token"] = raw_token
        return result
    finally:
        await client.aclose()


async def list_accounts(args: argparse.Namespace) -> dict[str, Any]:
    client = await connect(args.redis_url)
    try:
        prefix = f"{args.prefix}accounts:"
        records: list[AccountRecord] = []
        async for key in client.scan_iter(match=f"{prefix}*"):
            if key.endswith(":perms"):
                continue
            agent_id = key[len(prefix) :]
            record = await load_record(client, args.prefix, agent_id)
            if record:
                records.append(record)
        records.sort(key=lambda item: item.agent_id)
        return {"accounts": [record.as_dict() for record in records]}
    finally:
        await client.aclose()


async def show_account(args: argparse.Namespace) -> dict[str, Any]:
    agent_id = validate_agent_id(args.agent_id)
    client = await connect(args.redis_url)
    try:
        record = await load_record(client, args.prefix, agent_id)
        if record is None:
            raise AccountCliError(f"account {agent_id!r} does not exist")
        return record.as_dict()
    finally:
        await client.aclose()


async def rotate_token(args: argparse.Namespace) -> dict[str, Any]:
    agent_id = validate_agent_id(args.agent_id)
    raw_token = generate_token()
    hashed_token = token_hash(raw_token)
    client = await connect(args.redis_url)
    try:
        key = account_key(args.prefix, agent_id)
        account = await client.hgetall(key)
        if not account:
            raise AccountCliError(f"account {agent_id!r} does not exist")

        old_hash = account.get("token_hash")
        pipe = client.pipeline()
        pipe.hset(key, mapping={"token_hash": hashed_token, "updated_at": now_timestamp()})
        if old_hash:
            pipe.delete(token_index_key(args.prefix, old_hash))
        pipe.set(token_index_key(args.prefix, hashed_token), agent_id)
        await pipe.execute()
        await refresh_account_integrity(
            client,
            args.prefix,
            agent_id,
            args.account_integrity_key,
        )

        record = await load_record(client, args.prefix, agent_id)
        result = record.as_dict() if record else {}
        result["token"] = raw_token
        return result
    finally:
        await client.aclose()


async def set_enabled(args: argparse.Namespace, enabled: bool) -> dict[str, Any]:
    agent_id = validate_agent_id(args.agent_id)
    client = await connect(args.redis_url)
    try:
        key = account_key(args.prefix, agent_id)
        if not await client.exists(key):
            raise AccountCliError(f"account {agent_id!r} does not exist")
        await client.hset(
            key,
            mapping={"enabled": "1" if enabled else "0", "updated_at": now_timestamp()},
        )
        await refresh_account_integrity(
            client,
            args.prefix,
            agent_id,
            args.account_integrity_key,
        )
        record = await load_record(client, args.prefix, agent_id)
        return record.as_dict() if record else {}
    finally:
        await client.aclose()


async def set_permissions(args: argparse.Namespace) -> dict[str, Any]:
    agent_id = validate_agent_id(args.agent_id)
    permissions = resolve_permissions(args.role, args.permission, default_role=None)
    role = args.role or "custom"
    client = await connect(args.redis_url)
    try:
        key = account_key(args.prefix, agent_id)
        if not await client.exists(key):
            raise AccountCliError(f"account {agent_id!r} does not exist")

        pipe = client.pipeline()
        pipe.delete(perms_key(args.prefix, agent_id))
        if permissions:
            pipe.sadd(perms_key(args.prefix, agent_id), *permissions)
        pipe.hset(key, mapping={"role": role, "updated_at": now_timestamp()})
        await pipe.execute()
        await refresh_account_integrity(
            client,
            args.prefix,
            agent_id,
            args.account_integrity_key,
        )

        record = await load_record(client, args.prefix, agent_id)
        return record.as_dict() if record else {}
    finally:
        await client.aclose()


async def delete_account(args: argparse.Namespace) -> dict[str, Any]:
    agent_id = validate_agent_id(args.agent_id)
    if not args.yes:
        raise AccountCliError("delete requires --yes")
    client = await connect(args.redis_url)
    try:
        key = account_key(args.prefix, agent_id)
        account = await client.hgetall(key)
        if not account:
            raise AccountCliError(f"account {agent_id!r} does not exist")

        councils = await client.smembers(account_councils_key(args.prefix, agent_id))
        pipe = client.pipeline()
        pipe.delete(key)
        pipe.delete(perms_key(args.prefix, agent_id))
        pipe.delete(account_councils_key(args.prefix, agent_id))
        for council_id in councils:
            pipe.srem(council_agents_key(args.prefix, council_id), agent_id)
        if account.get("token_hash"):
            pipe.delete(token_index_key(args.prefix, account["token_hash"]))
        await pipe.execute()
        return {"deleted": True, "agent_id": agent_id}
    finally:
        await client.aclose()


async def grant_council(args: argparse.Namespace) -> dict[str, Any]:
    agent_id = validate_agent_id(args.agent_id)
    council_id = validate_council_id(args.council_id)
    client = await connect(args.redis_url)
    try:
        if not await client.exists(account_key(args.prefix, agent_id)):
            raise AccountCliError(f"account {agent_id!r} does not exist")
        pipe = client.pipeline()
        pipe.sadd(account_councils_key(args.prefix, agent_id), council_id)
        pipe.sadd(council_agents_key(args.prefix, council_id), agent_id)
        pipe.hset(account_key(args.prefix, agent_id), mapping={"updated_at": now_timestamp()})
        await pipe.execute()
        await refresh_account_integrity(
            client,
            args.prefix,
            agent_id,
            args.account_integrity_key,
        )
        record = await load_record(client, args.prefix, agent_id)
        return record.as_dict() if record else {}
    finally:
        await client.aclose()


async def revoke_council(args: argparse.Namespace) -> dict[str, Any]:
    agent_id = validate_agent_id(args.agent_id)
    council_id = validate_council_id(args.council_id)
    client = await connect(args.redis_url)
    try:
        if not await client.exists(account_key(args.prefix, agent_id)):
            raise AccountCliError(f"account {agent_id!r} does not exist")
        pipe = client.pipeline()
        pipe.srem(account_councils_key(args.prefix, agent_id), council_id)
        pipe.srem(council_agents_key(args.prefix, council_id), agent_id)
        pipe.hset(account_key(args.prefix, agent_id), mapping={"updated_at": now_timestamp()})
        await pipe.execute()
        await refresh_account_integrity(
            client,
            args.prefix,
            agent_id,
            args.account_integrity_key,
        )
        record = await load_record(client, args.prefix, agent_id)
        return record.as_dict() if record else {}
    finally:
        await client.aclose()


async def account_councils(args: argparse.Namespace) -> dict[str, Any]:
    agent_id = validate_agent_id(args.agent_id)
    client = await connect(args.redis_url)
    try:
        if not await client.exists(account_key(args.prefix, agent_id)):
            raise AccountCliError(f"account {agent_id!r} does not exist")
        councils = sorted(await client.smembers(account_councils_key(args.prefix, agent_id)))
        return {"agent_id": agent_id, "councils": councils}
    finally:
        await client.aclose()


async def council_members(args: argparse.Namespace) -> dict[str, Any]:
    council_id = validate_council_id(args.council_id)
    client = await connect(args.redis_url)
    try:
        agents = sorted(await client.smembers(council_agents_key(args.prefix, council_id)))
        return {"council_id": council_id, "agents": agents}
    finally:
        await client.aclose()


def print_text(command: str, result: dict[str, Any]) -> None:
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

    return parser


async def run(args: argparse.Namespace) -> int:
    try:
        result = await args.handler(args)
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
