# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import asyncio
import contextlib
import importlib.util
import io
import sys
from argparse import Namespace
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "agent_accounts.py"
SPEC = importlib.util.spec_from_file_location("agent_accounts", SCRIPT)
agent_accounts = importlib.util.module_from_spec(SPEC)
assert SPEC is not None
assert SPEC.loader is not None
sys.modules[SPEC.name] = agent_accounts
SPEC.loader.exec_module(agent_accounts)


def test_role_permissions_are_valid_permission_scopes():
    known = set(agent_accounts.ALL_PERMISSIONS)

    for permissions in agent_accounts.ROLE_PERMISSIONS.values():
        assert set(permissions) <= known


def test_default_permissions_are_developer_role():
    assert agent_accounts.resolve_permissions(None, []) == tuple(
        sorted(agent_accounts.ROLE_PERMISSIONS["developer"])
    )


def test_default_redis_url_can_come_from_environment(monkeypatch):
    monkeypatch.setenv("NOMOS_REDIS_URL", "redis://:secret@redis:6379/0")
    monkeypatch.setenv("REDIS_URL", "redis://:other@localhost:6379/0")

    assert agent_accounts.default_redis_url() == "redis://:secret@redis:6379/0"


def test_default_redis_url_falls_back_to_redis_url_environment(monkeypatch):
    monkeypatch.delenv("NOMOS_REDIS_URL", raising=False)
    monkeypatch.setenv("REDIS_URL", "redis://:secret@localhost:6379/0")

    assert agent_accounts.default_redis_url() == "redis://:secret@localhost:6379/0"


def test_redis_connection_config_reads_sentinel_environment(monkeypatch):
    monkeypatch.setenv("NOMOS_REDIS_MODE", "sentinel")
    monkeypatch.setenv("NOMOS_REDIS_SENTINEL_SERVICE_NAME", "nomos-master")
    monkeypatch.setenv("NOMOS_REDIS_SENTINEL_NODES", "sentinel-a:26379,sentinel-b:26379")
    monkeypatch.setenv("NOMOS_REDIS_CONNECT_TIMEOUT_MS", "250")
    monkeypatch.setenv("NOMOS_REDIS_COMMAND_TIMEOUT_MS", "750")

    config = agent_accounts.redis_connection_config("redis://:redis-secret@redis:6379/0")

    assert config.mode == "sentinel"
    assert config.url == "redis://:redis-secret@redis:6379/0"
    assert config.sentinel_service_name == "nomos-master"
    assert config.sentinel_nodes == ("sentinel-a:26379", "sentinel-b:26379")
    assert config.connect_timeout_ms == 250
    assert config.command_timeout_ms == 750


def test_redis_connection_config_falls_back_to_council_sentinel_environment(monkeypatch):
    monkeypatch.delenv("NOMOS_REDIS_MODE", raising=False)
    monkeypatch.delenv("NOMOS_REDIS_SENTINEL_SERVICE_NAME", raising=False)
    monkeypatch.delenv("NOMOS_REDIS_SENTINEL_NODES", raising=False)
    monkeypatch.setenv("COUNCIL_REDIS_MODE", "sentinel")
    monkeypatch.setenv("COUNCIL_REDIS_SENTINEL_SERVICE_NAME", "nomos-master")
    monkeypatch.setenv("COUNCIL_REDIS_SENTINEL_NODES", "sentinel-a:26379,sentinel-b:26379")

    config = agent_accounts.redis_connection_config("redis://:redis-secret@redis:6379/0")

    assert config.mode == "sentinel"
    assert config.sentinel_service_name == "nomos-master"
    assert config.sentinel_nodes == ("sentinel-a:26379", "sentinel-b:26379")


def test_default_prefix_can_come_from_environment(monkeypatch):
    monkeypatch.setenv("NOMOS_REDIS_PREFIX", "tenant:")

    assert agent_accounts.default_prefix() == "tenant:"


def test_default_council_can_come_from_environment(monkeypatch):
    monkeypatch.setenv("NOMOS_DEFAULT_COUNCIL_ID", "council-dev")

    assert agent_accounts.default_council_id() == "council-dev"


def test_default_council_falls_back_to_prod(monkeypatch):
    monkeypatch.delenv("NOMOS_DEFAULT_COUNCIL_ID", raising=False)
    monkeypatch.delenv("COUNCIL_DEFAULT_COUNCIL_ID", raising=False)

    assert agent_accounts.default_council_id() == "council-prod"


def test_explicit_permissions_make_custom_scope():
    permissions = agent_accounts.resolve_permissions(None, ["file:write", "shell:read"])

    assert permissions == ("file:write", "shell:read")


def test_permissions_can_be_required_for_update_commands():
    with pytest.raises(agent_accounts.AccountCliError, match="provide --role"):
        agent_accounts.resolve_permissions(None, [], default_role=None)


def test_role_and_explicit_permissions_are_mutually_exclusive():
    with pytest.raises(agent_accounts.AccountCliError, match="either --role or --permission"):
        agent_accounts.resolve_permissions("readonly", ["shell:read"])


def test_invalid_permission_is_rejected():
    with pytest.raises(agent_accounts.AccountCliError, match="unknown permission"):
        agent_accounts.validate_permissions(["shell:read", "database:drop"])


@pytest.mark.parametrize("agent_id", ["alice", "agent-1", "agent_alpha", "A12"])
def test_valid_agent_ids(agent_id):
    assert agent_accounts.validate_agent_id(agent_id) == agent_id


@pytest.mark.parametrize("council_id", ["council-prod", "infra.prod", "team_a", "*"])
def test_valid_council_ids(council_id):
    assert agent_accounts.validate_council_id(council_id) == council_id


@pytest.mark.parametrize("agent_id", ["alice@example.com", "../agent", "agent:prod", ""])
def test_invalid_agent_ids(agent_id):
    with pytest.raises(agent_accounts.AccountCliError, match="agent_id"):
        agent_accounts.validate_agent_id(agent_id)


@pytest.mark.parametrize("council_id", ["../prod", "prod:admin", "prod/security", ""])
def test_invalid_council_ids(council_id):
    with pytest.raises(agent_accounts.AccountCliError, match="council_id"):
        agent_accounts.validate_council_id(council_id)


def test_build_account_mapping_preserves_auth_fields():
    mapping = agent_accounts.build_account_mapping(
        token_hash_value="abc123",
        agent_id="agent-1",
        display_name="Agent One",
        enabled=False,
        role="readonly",
        created_at="1.0",
        updated_at="2.0",
    )

    assert mapping == {
        "token_hash": "abc123",
        "display_name": "Agent One",
        "created_at": "1.0",
        "updated_at": "2.0",
        "enabled": "0",
        "role": "readonly",
    }


class _FakeAccountRedis:
    def __init__(self, account, token_index):
        self.account = account
        self.token_index = token_index

    async def hgetall(self, _key):
        return dict(self.account)

    async def get(self, key):
        return self.token_index.get(key)

    async def aclose(self):
        pass


def test_verify_token_uses_constant_time_hash_comparison_without_exposing_secrets(
    monkeypatch,
):
    raw_token = "protected-candidate-token"  # pragma: allowlist secret
    stored_hash = agent_accounts.token_hash(raw_token)
    client = _FakeAccountRedis(
        {"token_hash": stored_hash, "enabled": "1"},
        {agent_accounts.token_index_key("nomos:", stored_hash): "agent-test"},
    )

    async def _connect(_url):
        return client

    monkeypatch.setattr(agent_accounts, "connect", _connect)
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw_token))
    args = Namespace(
        agent_id="agent-test",
        prefix="nomos:",
        redis_url="redis://unused",
        token_stdin=True,
    )

    assert asyncio.run(agent_accounts.verify_token(args)) == {
        "agent_id": "agent-test",
        "verified": True,
    }

    monkeypatch.setattr(sys, "stdin", io.StringIO("wrong-nonempty-token"))
    with pytest.raises(agent_accounts.AccountCliError) as exc:
        asyncio.run(agent_accounts.verify_token(args))
    message = str(exc.value)
    assert raw_token not in message
    assert stored_hash not in message


def test_account_commands_refuse_postgres_storage_mode(monkeypatch):
    monkeypatch.setenv("COUNCIL_STORAGE_MODE", "postgres")
    monkeypatch.setattr(sys, "argv", ["agent_accounts.py", "list"])
    import scripts.agent_accounts as cli

    with pytest.raises(SystemExit, match="account commands do not support"):
        cli.main()


def test_trusted_executor_commands_allowed_in_postgres_mode(monkeypatch):
    monkeypatch.setenv("COUNCIL_STORAGE_MODE", "postgres")
    monkeypatch.setattr(
        sys,
        "argv",
        ["agent_accounts.py", "trusted-executor", "list", "--database-url", "postgresql://x"],
    )
    import scripts.agent_accounts as cli

    store = _FakeStore()

    @contextlib.asynccontextmanager
    async def _open(_args):
        yield store

    monkeypatch.setattr(cli, "open_durable_store", _open)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0  # not blocked by the account guard; ran the durable store


# ── trusted-executor registration ──────────────────────────────────────────

# pubkey/fingerprint of the live openshell executor (sha256(pubkey) == FP).
_PUBHEX = (
    "7e3ec59e3ee4486f208c060d9884a4bd"  # pragma: allowlist secret
    "7414db9e7d92958a140f44ce5cb21cc7"  # pragma: allowlist secret
)
_FP = (
    "2e363bc682be3b5ebd497287890d8c967"  # pragma: allowlist secret
    "da8a678d68c06bf6da618a4c0b8ceab"  # pragma: allowlist secret
)


class _FakeStore:
    """Durable-store stand-in mirroring the trusted_executor record shape."""

    def __init__(self):
        self.recs: dict[str, dict] = {}

    async def register_trusted_executor(
        self, *, executor_id, public_key_hex, key_fingerprint, metadata=None, enabled=True
    ):
        import json

        self.recs[key_fingerprint] = {
            "executor_id": executor_id,
            "public_key_hex": public_key_hex,
            "key_fingerprint": key_fingerprint,
            "enabled": "1" if enabled else "0",
            "registered_at": "0",
            "updated_at": "0",
            "metadata_json": json.dumps(metadata or {}, sort_keys=True),
        }

    async def get_trusted_executor(self, fp):
        return self.recs.get(fp)

    async def list_trusted_executors(self):
        return [self.recs[k] for k in sorted(self.recs)]


class _FakeRedis:
    def __init__(self, data):
        self.data = data

    async def smembers(self, key):
        return set(self.data.get(key, set()))

    async def hgetall(self, key):
        return dict(self.data.get(key, {}))

    async def aclose(self):
        pass


def _patch_store(monkeypatch, store):
    @contextlib.asynccontextmanager
    async def _open(_args):
        yield store

    monkeypatch.setattr(agent_accounts, "open_durable_store", _open)


def _reg_args(**overrides):
    base = dict(
        database_url="postgresql://x",
        redis_url="",
        prefix="nomos:",
        from_redis=False,
        fingerprint="",
        metadata_json="{}",
        disabled=False,
        executor_id="openshell:2e363bc682be3b5e",
        public_key_hex=_PUBHEX,
        public_key_file="",
    )
    base.update(overrides)
    return Namespace(**base)


def test_te_register_explicit_computes_fingerprint(monkeypatch):
    store = _FakeStore()
    _patch_store(monkeypatch, store)
    res = asyncio.run(agent_accounts.trusted_executor_register(_reg_args()))
    assert res["count"] == 1
    rec = res["registered"][0]
    assert rec["key_fingerprint"] == _FP
    assert rec["enabled"] == "1"
    assert rec["public_key_hex"] == _PUBHEX
    assert store.recs[_FP]["executor_id"] == "openshell:2e363bc682be3b5e"


def test_te_register_disabled(monkeypatch):
    store = _FakeStore()
    _patch_store(monkeypatch, store)
    res = asyncio.run(agent_accounts.trusted_executor_register(_reg_args(disabled=True)))
    assert res["registered"][0]["enabled"] == "0"


def test_te_register_rejects_bad_public_key(monkeypatch):
    store = _FakeStore()
    _patch_store(monkeypatch, store)
    with pytest.raises(agent_accounts.AccountCliError, match="32 bytes"):
        asyncio.run(agent_accounts.trusted_executor_register(_reg_args(public_key_hex="aa" * 31)))


def test_te_register_requires_id_unless_from_redis(monkeypatch):
    store = _FakeStore()
    _patch_store(monkeypatch, store)
    with pytest.raises(agent_accounts.AccountCliError, match="executor_id is required"):
        asyncio.run(agent_accounts.trusted_executor_register(_reg_args(executor_id=None)))


def test_te_list(monkeypatch):
    store = _FakeStore()
    asyncio.run(
        store.register_trusted_executor(
            executor_id="openshell:2e363bc682be3b5e",
            public_key_hex=_PUBHEX,
            key_fingerprint=_FP,
            enabled=True,
        )
    )
    _patch_store(monkeypatch, store)
    res = asyncio.run(
        agent_accounts.trusted_executor_list(
            Namespace(database_url="postgresql://x", redis_url="", prefix="nomos:")
        )
    )
    assert len(res["trusted_executors"]) == 1
    assert res["trusted_executors"][0]["key_fingerprint"] == _FP


def test_te_register_from_redis_syncs_to_durable_store(monkeypatch):
    store = _FakeStore()
    source = _FakeRedis(
        {
            "nomos:trusted_executors": {_FP},
            "nomos:trusted_executors:" + _FP: {
                "executor_id": "openshell:2e363bc682be3b5e",
                "public_key_hex": _PUBHEX,
                "key_fingerprint": _FP,
                "enabled": "1",
                "metadata_json": "{}",
            },
        }
    )

    async def _connect(_url):
        return source

    monkeypatch.setattr(agent_accounts, "connect", _connect)
    _patch_store(monkeypatch, store)
    res = asyncio.run(
        agent_accounts.trusted_executor_register(
            _reg_args(from_redis=True, executor_id=None, public_key_hex="")
        )
    )
    assert res["count"] == 1
    assert store.recs[_FP]["executor_id"] == "openshell:2e363bc682be3b5e"
    assert store.recs[_FP]["enabled"] == "1"
