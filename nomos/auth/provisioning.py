# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Agent account provisioning — the single implementation.

Every account mutation in Nomos goes through this module: the accounts CLI
(`scripts/agent_accounts.py`), the signup console (`agent_signup/server.py`),
and the deploy registration wrappers. It operates on any ``DurableStore``, so
the same code serves both the Redis and PostgreSQL backends.

Previously each entry point reimplemented the write sequence against its own
backend, and each independently had to remember to sign the account integrity
envelope. One of them did not, producing accounts that stored fine and then
failed authentication.

Write ordering
--------------
``DurableStore`` exposes no transaction or batch surface, so a multi-step
mutation cannot be atomic. Ordering is therefore chosen so that a partial write
fails *closed* rather than half-authenticating:

``AgentAuthenticator.authenticate`` resolves the caller by looking the token
hash up in the token index first. So the token index is written **last** on
create and deleted **first** on delete — an interrupted create leaves an
account nothing can find, and an interrupted delete revokes access before the
record goes away.

Every mutation reads back and verifies the integrity envelope before returning,
which is what catches a store constructed without an integrity key.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from nomos.auth.permissions import permissions_for_role, validate_permissions


class ProvisioningError(Exception):
    """Expected, user-facing account provisioning failure."""


class AccountExistsError(ProvisioningError):
    """The account already exists and ``force`` was not set.

    A distinct type because callers must tell "the caller asked for something
    that is already there" (a 409) from "we could not write" (a 500). The signup
    console previously decided that by substring-matching the message, which is a
    contract nothing enforces.
    """


class PermissionsRequiredError(ProvisioningError):
    """Neither a role nor an explicit permission set was supplied."""


class AccountStore(Protocol):
    """The `DurableStore` subset this module needs.

    Declared structurally so callers can pass a `RedisStore`, a `PostgresStore`,
    or a test double without importing either concrete backend here.
    """

    async def set_account(
        self, agent_id: str, token_hash: str, display_name: str = "", enabled: bool = True
    ) -> None: ...
    async def get_account(self, agent_id: str) -> dict[str, str] | None: ...
    async def set_account_permissions(self, agent_id: str, permissions: set[str]) -> None: ...
    async def get_account_permissions(self, agent_id: str) -> set[str]: ...
    async def set_account_councils(self, agent_id: str, councils: set[str]) -> None: ...
    async def grant_account_council(self, agent_id: str, council_id: str) -> None: ...
    async def revoke_account_council(self, agent_id: str, council_id: str) -> None: ...
    async def get_account_councils(self, agent_id: str) -> set[str]: ...
    async def get_council_agents(self, council_id: str) -> set[str]: ...
    async def find_agent_by_token_hash(self, token_hash: str) -> str | None: ...
    async def set_token_index(self, token_hash: str, agent_id: str) -> None: ...
    async def update_account_fields(self, agent_id: str, fields: dict[str, str]) -> None: ...
    async def delete_account(self, agent_id: str) -> None: ...
    async def delete_token_index(self, token_hash: str) -> None: ...
    async def list_account_ids(self) -> list[str]: ...
    def verify_account_integrity(
        self,
        agent_id: str,
        account: dict[str, str],
        permissions: set[str],
        councils: set[str],
    ) -> bool: ...


@dataclass(frozen=True)
class AccountRecord:
    """A rendered account, shared by every caller's ``--json`` output."""

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


def generate_token() -> str:
    return secrets.token_hex(32)


def token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


def now_timestamp() -> str:
    return str(time.time())


def resolve_permissions(
    role: str | None,
    explicit: Sequence[str],
    *,
    default_role: str | None = "developer",
) -> tuple[str, ...]:
    """Resolve a role and/or explicit permission list into permission scopes."""
    if role and explicit:
        raise ProvisioningError("use either a role or explicit permissions, not both")
    if explicit:
        return validate_permissions(explicit)
    if role or default_role:
        return validate_permissions(permissions_for_role(role or default_role or ""))
    raise ProvisioningError("provide a role or at least one permission")


async def load_record(store: AccountStore, agent_id: str) -> AccountRecord | None:
    """Read an account and render it, or return None when it does not exist."""
    account = await store.get_account(agent_id)
    if not account:
        return None
    permissions = sorted(await store.get_account_permissions(agent_id))
    councils = sorted(await store.get_account_councils(agent_id))
    stored_hash = account.get("token_hash", "")
    return AccountRecord(
        agent_id=agent_id,
        display_name=account.get("display_name", "") or agent_id,
        enabled=account.get("enabled") == "1",
        permissions=tuple(permissions),
        councils=tuple(councils),
        role=account.get("role", ""),
        created_at=account.get("created_at", ""),
        updated_at=account.get("updated_at", ""),
        token_hash_prefix=f"{stored_hash[:16]}..." if stored_hash else "",
    )


async def _assert_integrity(store: AccountStore, agent_id: str) -> AccountRecord:
    """Read the account back and confirm its integrity envelope verifies.

    A store built without an account integrity key writes no envelope, and
    `verify_account_integrity` then returns True unconditionally — so this
    cannot detect an unkeyed store on its own. `require_signed_store` covers
    that case; this covers a write that landed inconsistently.
    """
    account = await store.get_account(agent_id)
    if account is None:
        raise ProvisioningError(f"account {agent_id!r} vanished during provisioning")
    permissions = await store.get_account_permissions(agent_id)
    councils = await store.get_account_councils(agent_id)
    if not store.verify_account_integrity(agent_id, account, permissions, councils):
        raise ProvisioningError(
            f"account {agent_id!r} failed integrity verification after write; "
            "the council would reject this account at login"
        )
    record = await load_record(store, agent_id)
    if record is None:  # pragma: no cover - get_account already succeeded
        raise ProvisioningError(f"account {agent_id!r} vanished during provisioning")
    return record


async def create_account(
    store: AccountStore,
    *,
    agent_id: str,
    role: str | None = None,
    display_name: str = "",
    councils: Iterable[str] = (),
    permissions: Sequence[str] = (),
    force: bool = False,
    enabled: bool = True,
) -> dict[str, Any]:
    """Create (or, with force, replace) an account and return it plus a raw token.

    The returned dict carries a top-level ``token`` key; `scripts/deploy_stack.sh`
    parses exactly that.

    ``enabled=False`` mints an inert account: the raw token is returned, but
    `AgentAuthenticator` rejects it until the account is enabled. The enabled flag
    is written by `set_account` below, which runs before the token index is
    published, so a disabled account is never briefly authenticable.
    """
    # Normalize to ProvisioningError. resolve_permissions raises ValueError from
    # nomos.auth.permissions, so without this a caller has to catch two unrelated
    # exception types to handle one failure -- and the signup console caught only
    # one of them.
    if not role and not permissions:
        raise PermissionsRequiredError(
            "create_account requires an explicit role or permissions; refusing to "
            "guess a privilege level for a new account"
        )
    try:
        resolved_permissions = resolve_permissions(role, permissions)
    except ValueError as exc:
        raise ProvisioningError(str(exc)) from exc
    council_ids = {c for c in councils if c}
    raw_token = generate_token()
    hashed_token = token_hash(raw_token)
    now = now_timestamp()

    existing = await store.get_account(agent_id)
    if existing and not force:
        raise AccountExistsError(f"account {agent_id!r} already exists (use force to replace)")

    # Preserve the original creation time when replacing; set_account rewrites it.
    created_at = existing.get("created_at", now) if existing else now
    old_hash = existing.get("token_hash", "") if existing else ""

    # Unpublish first: no account is mutated while a token index entry still
    # points at it, so a replace can never be observed half-applied.
    if old_hash:
        await store.delete_token_index(old_hash)

    await store.set_account(agent_id, hashed_token, display_name or agent_id, enabled)
    await store.update_account_fields(
        agent_id,
        {"role": role or "", "created_at": created_at, "updated_at": now},
    )
    await store.set_account_permissions(agent_id, set(resolved_permissions))
    await store.set_account_councils(agent_id, council_ids)

    # Verify BEFORE publishing. An account that cannot prove it will authenticate
    # must never become reachable, and its raw token must never be printed.
    record = await _assert_integrity(store, agent_id)

    # Publish last: until this lands the account is unfindable, so an interrupted
    # create leaves something inert rather than something half-authenticating.
    await store.set_token_index(hashed_token, agent_id)

    result = record.as_dict()
    result["token"] = raw_token
    return result


async def rotate_token(store: AccountStore, *, agent_id: str) -> dict[str, Any]:
    """Mint a fresh token for an existing account and return it.

    The new index entry is written before the old one is removed, so there is no
    instant where the account has no valid token.
    """
    account = await store.get_account(agent_id)
    if account is None:
        raise ProvisioningError(f"account {agent_id!r} does not exist")

    raw_token = generate_token()
    hashed_token = token_hash(raw_token)
    old_hash = account.get("token_hash", "")

    await store.set_token_index(hashed_token, agent_id)
    await store.update_account_fields(
        agent_id, {"token_hash": hashed_token, "updated_at": now_timestamp()}
    )
    if old_hash and old_hash != hashed_token:
        await store.delete_token_index(old_hash)

    record = await _assert_integrity(store, agent_id)
    result = record.as_dict()
    result["token"] = raw_token
    return result


async def set_enabled(store: AccountStore, *, agent_id: str, enabled: bool) -> dict[str, Any]:
    if await store.get_account(agent_id) is None:
        raise ProvisioningError(f"account {agent_id!r} does not exist")
    await store.update_account_fields(
        agent_id, {"enabled": "1" if enabled else "0", "updated_at": now_timestamp()}
    )
    return (await _assert_integrity(store, agent_id)).as_dict()


async def set_permissions(
    store: AccountStore,
    *,
    agent_id: str,
    role: str | None = None,
    permissions: Sequence[str] = (),
) -> dict[str, Any]:
    if await store.get_account(agent_id) is None:
        raise ProvisioningError(f"account {agent_id!r} does not exist")
    resolved = resolve_permissions(role, permissions, default_role=None)
    await store.set_account_permissions(agent_id, set(resolved))
    await store.update_account_fields(agent_id, {"role": role or "", "updated_at": now_timestamp()})
    return (await _assert_integrity(store, agent_id)).as_dict()


async def grant_council(store: AccountStore, *, agent_id: str, council_id: str) -> dict[str, Any]:
    if await store.get_account(agent_id) is None:
        raise ProvisioningError(f"account {agent_id!r} does not exist")
    await store.grant_account_council(agent_id, council_id)
    await store.update_account_fields(agent_id, {"updated_at": now_timestamp()})
    return (await _assert_integrity(store, agent_id)).as_dict()


async def revoke_council(store: AccountStore, *, agent_id: str, council_id: str) -> dict[str, Any]:
    if await store.get_account(agent_id) is None:
        raise ProvisioningError(f"account {agent_id!r} does not exist")
    await store.revoke_account_council(agent_id, council_id)
    await store.update_account_fields(agent_id, {"updated_at": now_timestamp()})
    return (await _assert_integrity(store, agent_id)).as_dict()


async def delete_account(store: AccountStore, *, agent_id: str) -> dict[str, Any]:
    """Delete an account, revoking its token before the record disappears."""
    account = await store.get_account(agent_id)
    if account is None:
        raise ProvisioningError(f"account {agent_id!r} does not exist")

    # First: revoke reachability, so an interrupted delete cannot leave a
    # usable token pointing at a partially-removed account.
    stored_hash = account.get("token_hash", "")
    if stored_hash:
        await store.delete_token_index(stored_hash)
    await store.delete_account(agent_id)
    return {"deleted": True, "agent_id": agent_id}


async def account_councils(store: AccountStore, *, agent_id: str) -> dict[str, Any]:
    if await store.get_account(agent_id) is None:
        raise ProvisioningError(f"account {agent_id!r} does not exist")
    return {"agent_id": agent_id, "councils": sorted(await store.get_account_councils(agent_id))}


async def council_members(store: AccountStore, *, council_id: str) -> dict[str, Any]:
    return {"council_id": council_id, "agents": sorted(await store.get_council_agents(council_id))}


async def list_accounts(store: AccountStore) -> list[AccountRecord]:
    """List every account, sorted by agent id."""
    records: list[AccountRecord] = []
    for agent_id in sorted(await store.list_account_ids()):
        record = await load_record(store, agent_id)
        if record is not None:
            records.append(record)
    return records


def require_signed_store(store: Any, *, storage_mode: str, allow_unsigned: bool = False) -> str:
    """Check that the store can sign accounts; return a warning or raise.

    An unkeyed store writes no `_integrity_hmac`, and `verify_account_integrity`
    then passes unconditionally — so the failure is silent at write time and only
    surfaces later as a rejected login against a council that *is* keyed.

    The two backends are treated differently on purpose. Every shipped postgres
    deployment configures the key, and provisioning without it there is always a
    mistake, so it hard-fails. An empty key is still a legitimate local Redis
    configuration (``deploy/docker-compose.yml`` passes
    ``${COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY:-}``), so that path only warns —
    hard-failing it would break working dev flows.

    Returns a warning string (empty when there is nothing to report).
    """
    if getattr(store, "_account_integrity_key", None):
        return ""

    message = (
        "no account integrity key is configured: accounts will be written "
        "unsigned and rejected by any council that has one. Set "
        "NOMOS_ACCOUNT_INTEGRITY_KEY or COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY."
    )
    if storage_mode == "postgres" and not allow_unsigned:
        raise ProvisioningError(f"refusing to provision accounts — {message}")
    return message
