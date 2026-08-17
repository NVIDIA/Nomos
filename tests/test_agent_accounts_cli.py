# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import asyncio
import contextlib
import importlib.util
import io
import re
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from nomos.auth.account_integrity import (
    ACCOUNT_INTEGRITY_FIELD,
    sign_account_record,
    verify_account_record,
)

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


def test_account_commands_are_allowed_in_postgres_storage_mode(monkeypatch):
    """Account commands must work under COUNCIL_STORAGE_MODE=postgres.

    They used to be refused outright, which left `deploy_stack.sh` unable to mint
    the first agent on a Kind cluster (postgres is the chart default) — the stack
    came up with an empty accounts table and no way to authenticate.
    """
    monkeypatch.setenv("COUNCIL_STORAGE_MODE", "postgres")
    monkeypatch.setattr(
        sys, "argv", ["agent_accounts.py", "list", "--database-url", "postgresql://x"]
    )
    import scripts.agent_accounts as cli

    store = _FakeStore()

    @contextlib.asynccontextmanager
    async def _open(_args):
        yield store

    monkeypatch.setattr(cli, "open_durable_store", _open)

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 0


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

    # Account surface — enough for the CLI's read paths to run against this
    # double. Full account behaviour is covered against real backends in
    # tests/test_account_provisioning.py.
    async def list_account_ids(self):
        return []

    async def get_account(self, agent_id):
        return None

    async def get_account_permissions(self, agent_id):
        return set()

    async def get_account_councils(self, agent_id):
        return set()

    def verify_account_integrity(self, agent_id, account, permissions, councils):
        return True

    # Non-empty so require_signed_store() treats this double as key-configured.
    _account_integrity_key = "fake-store-integrity-key"


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


def _redis_args(url: str) -> Namespace:
    return Namespace(redis_url=url, prefix="nomos:", account_integrity_key="k" * 32)


def test_deployment_redis_password_beats_the_dev_default(monkeypatch):
    """The env password must win when no password was given in the URL.

    The fallback URL used to embed "nomos-dev". Because that made the URL
    password non-empty, COUNCIL_REDIS_PASSWORD was never consulted, and every
    invocation against a real deployment authenticated with the dev password and
    failed. In sentinel mode this surfaced only as MasterNotFoundError, since the
    same wrong password is what authenticates to the sentinels.
    """
    monkeypatch.delenv("NOMOS_REDIS_PASSWORD", raising=False)
    monkeypatch.setenv(
        "COUNCIL_REDIS_PASSWORD", "generated-deployment-password"
    )  # pragma: allowlist secret

    assert "nomos-dev" not in agent_accounts.FALLBACK_REDIS_URL  # pragma: allowlist secret

    config = agent_accounts.redis_config_from_args(_redis_args(agent_accounts.FALLBACK_REDIS_URL))
    assert config.password == "generated-deployment-password"  # pragma: allowlist secret


def test_explicit_url_password_still_wins(monkeypatch):
    monkeypatch.setenv("COUNCIL_REDIS_PASSWORD", "from-env")  # pragma: allowlist secret

    config = agent_accounts.redis_config_from_args(
        _redis_args("redis://:from-url@redis:6379/0")  # pragma: allowlist secret
    )
    assert config.password == "from-url"  # pragma: allowlist secret


def test_dev_default_password_survives_with_no_configuration(monkeypatch):
    monkeypatch.delenv("NOMOS_REDIS_PASSWORD", raising=False)
    monkeypatch.delenv("COUNCIL_REDIS_PASSWORD", raising=False)

    config = agent_accounts.redis_config_from_args(_redis_args(agent_accounts.FALLBACK_REDIS_URL))
    assert config.password == "nomos-dev"  # pragma: allowlist secret


def test_trusted_executor_redis_path_resolves_the_same_password(monkeypatch):
    """`connect()` builds its config separately and must not regress either."""
    monkeypatch.delenv("NOMOS_REDIS_PASSWORD", raising=False)
    monkeypatch.setenv(
        "COUNCIL_REDIS_PASSWORD", "generated-deployment-password"
    )  # pragma: allowlist secret

    config = agent_accounts.redis_connection_config(agent_accounts.FALLBACK_REDIS_URL)
    assert config.password == "generated-deployment-password"  # pragma: allowlist secret


def test_create_disabled_flag_is_honoured():
    """`create --disabled` must not mint an authenticating account.

    The flag was registered on the parser and then never read, and
    provisioning.create_account had no `enabled` parameter, so it always wrote
    enabled=True. An operator asking for an inert account received one holding a
    live, working token -- a fail-open outcome in a privileged-action broker.
    """
    import inspect

    from nomos.auth import provisioning

    signature = inspect.signature(provisioning.create_account)
    assert "enabled" in signature.parameters, "create_account cannot express a disabled account"

    source = inspect.getsource(agent_accounts.create_account)
    assert "enabled=" in source, "the CLI create handler must pass enabled through"
    assert "args.default_council" in source or "default_council" in source, (
        "--default-council was parsed and then ignored"
    )


def test_connect_errors_do_not_leak_the_dsn_password(monkeypatch):
    """Driver errors quote the connection string, and those errors now reach logs.

    compose-register-agent.sh prints stderr and deploy_stack.sh captures it, so an
    unredacted psycopg/redis error puts COUNCIL_DATABASE_URL's password into deploy
    and CI output.
    """
    monkeypatch.setenv(
        "COUNCIL_DATABASE_URL",
        "postgresql://nomos:sup3rs3cret@db:5432/nomos",  # pragma: allowlist secret
    )
    dsn = "postgresql://nomos:sup3rs3cret@db:5432/nomos"  # pragma: allowlist secret
    exc = RuntimeError(f'connection failed: "{dsn}"')

    rendered = agent_accounts.redact_secrets(exc, dsn)

    assert "sup3rs3cret" not in rendered
    # Still actionable: host and database survive.
    assert "db:5432" in rendered
    assert "nomos" in rendered


class _FakeAccountStore:
    """Agent-addressed fake, so a read for the wrong agent is detectable.

    Implements the ``AccountStore`` methods ``verify_token`` uses. Accounts moved
    behind that protocol so the CLI answers the same under Redis and Postgres, and
    the fake follows: keying this on raw Redis keys again would test a shape the
    Kind deployment no longer uses.
    """

    def __init__(
        self,
        *,
        accounts=None,
        token_index=None,
        permissions=None,
        councils=None,
        integrity_key="",
        error=None,
    ):
        self.accounts = accounts or {}
        self.token_index = token_index or {}
        self.permissions = permissions or {}
        self.councils = councils or {}
        self.integrity_key = integrity_key
        self.error = error

    def _check(self):
        if self.error is not None:
            raise self.error

    async def get_account(self, agent_id):
        self._check()
        account = self.accounts.get(agent_id)
        return dict(account) if account else None

    async def find_agent_by_token_hash(self, token_hash):
        self._check()
        return self.token_index.get(token_hash)

    async def get_account_permissions(self, agent_id):
        self._check()
        return set(self.permissions.get(agent_id, set()))

    async def get_account_councils(self, agent_id):
        self._check()
        return set(self.councils.get(agent_id, set()))

    def verify_account_integrity(self, agent_id, account, permissions, councils):
        # An unkeyed store passes vacuously, exactly as the real ones do.
        if not self.integrity_key:
            return True
        return verify_account_record(
            key=self.integrity_key,
            agent_id=agent_id,
            account=account,
            permissions=permissions,
            councils=councils,
        )


_VERIFY_PREFIX = "nomos:"
_VERIFY_AGENT = "agent-test"
_VERIFY_INTEGRITY_KEY = "integrity-key-for-tests"  # pragma: allowlist secret


def _verify_args(**overrides):
    args = {
        "agent_id": _VERIFY_AGENT,
        "prefix": _VERIFY_PREFIX,
        "redis_url": "redis://unused",
        "token_stdin": True,
        "expect_council": None,
        "account_integrity_key": _VERIFY_INTEGRITY_KEY,
    }
    args.update(overrides)
    return Namespace(**args)


def _verify_client(
    *,
    raw_token,
    enabled="1",
    indexed_agent_id=_VERIFY_AGENT,
    councils=("council-prod",),
    permissions=("shell:read",),
    account_present=True,
    sign_integrity=True,
    error=None,
):
    """Build a fake store whose integrity envelope is valid unless asked otherwise."""
    stored_hash = agent_accounts.token_hash(raw_token)
    account = {
        "token_hash": stored_hash,
        "enabled": enabled,
        "display_name": _VERIFY_AGENT,
    }
    if sign_integrity:
        account[ACCOUNT_INTEGRITY_FIELD] = sign_account_record(
            key=_VERIFY_INTEGRITY_KEY,
            agent_id=_VERIFY_AGENT,
            account=account,
            permissions=set(permissions),
            councils=set(councils),
        )
    else:
        account[ACCOUNT_INTEGRITY_FIELD] = "0" * 64
    return _FakeAccountStore(
        accounts={_VERIFY_AGENT: account} if account_present else {},
        token_index=({stored_hash: indexed_agent_id} if indexed_agent_id is not None else {}),
        permissions={_VERIFY_AGENT: set(permissions)},
        councils={_VERIFY_AGENT: set(councils)},
        integrity_key=_VERIFY_INTEGRITY_KEY,
        error=error,
    )


def _run_verify(monkeypatch, client, stdin_value, args=None):
    @contextlib.asynccontextmanager
    async def _open_store(_args):
        yield client

    # Deleted rather than left to the ambient environment: with it set to
    # postgres, verify_token's --database-url precondition fires and every case
    # below fails for a reason none of them is about.
    monkeypatch.delenv("COUNCIL_STORAGE_MODE", raising=False)
    monkeypatch.setattr(agent_accounts, "open_durable_store", _open_store)
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_value))
    return asyncio.run(agent_accounts.verify_token(args or _verify_args()))


def test_verify_token_uses_constant_time_hash_comparison_without_exposing_secrets(
    monkeypatch,
):
    raw_token = "protected-candidate-token"  # pragma: allowlist secret
    stored_hash = agent_accounts.token_hash(raw_token)
    client = _verify_client(raw_token=raw_token)

    assert _run_verify(monkeypatch, client, raw_token) == {
        "agent_id": _VERIFY_AGENT,
        "verified": True,
        "integrity_checked": True,
    }

    with pytest.raises(agent_accounts.AccountCliError) as exc:
        _run_verify(monkeypatch, client, "wrong-nonempty-token")
    message = str(exc.value)
    assert raw_token not in message
    assert stored_hash not in message


def test_verify_token_ignores_surrounding_whitespace_from_stdin(monkeypatch):
    """A trailing newline from a shell pipeline must not fail an otherwise valid token."""
    raw_token = "protected-candidate-token"  # pragma: allowlist secret
    client = _verify_client(raw_token=raw_token)

    assert _run_verify(monkeypatch, client, f"  {raw_token}\n") == {
        "agent_id": _VERIFY_AGENT,
        "verified": True,
        "integrity_checked": True,
    }

    with pytest.raises(agent_accounts.AccountCliError, match="empty"):
        _run_verify(monkeypatch, client, "   \n")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"enabled": "0"},
        {"account_present": False},
        {"indexed_agent_id": "someone-else"},
        {"indexed_agent_id": None},
    ],
    ids=["disabled", "absent", "index-mismatch", "index-missing"],
)
def test_verify_token_denies(monkeypatch, kwargs):
    raw_token = "protected-candidate-token"  # pragma: allowlist secret
    client = _verify_client(raw_token=raw_token, **kwargs)

    with pytest.raises(agent_accounts.AccountCliError) as exc:
        _run_verify(monkeypatch, client, raw_token)
    assert raw_token not in str(exc.value)


def test_verify_token_rejects_a_valid_token_with_a_broken_integrity_envelope(monkeypatch):
    """Production authenticate() verifies the envelope, so this check must too.

    Without it a deployment reports a healthy credential that live MCP
    authentication then rejects for a stale or forged integrity HMAC.
    """
    raw_token = "protected-candidate-token"  # pragma: allowlist secret
    client = _verify_client(raw_token=raw_token, sign_integrity=False)

    with pytest.raises(agent_accounts.AccountCliError, match="integrity"):
        _run_verify(monkeypatch, client, raw_token)


def test_verify_token_requires_expected_council_membership(monkeypatch):
    """Reuse must not be reported healthy for an account in a different council."""
    raw_token = "protected-candidate-token"  # pragma: allowlist secret
    client = _verify_client(raw_token=raw_token, councils=("council-other",))

    with pytest.raises(agent_accounts.AccountCliError, match="council-prod"):
        _run_verify(
            monkeypatch,
            client,
            raw_token,
            args=_verify_args(expect_council=["council-prod"]),
        )

    # Comma-separated form, as AGENT_COUNCILS supplies it.
    member = _verify_client(raw_token=raw_token, councils=("council-prod", "council-two"))
    assert _run_verify(
        monkeypatch,
        member,
        raw_token,
        args=_verify_args(expect_council=["council-prod,council-two"]),
    ) == {"agent_id": _VERIFY_AGENT, "verified": True, "integrity_checked": True}


def test_verify_token_without_an_integrity_key_reports_the_weaker_check(monkeypatch):
    """No configured key means the envelope is not checked — say so in the result.

    The account below carries a deliberately invalid envelope: it still verifies,
    because there is no key to check it with, so the caller must be able to tell
    this apart from a fully verified account.
    """
    raw_token = "protected-candidate-token"  # pragma: allowlist secret
    client = _verify_client(raw_token=raw_token, sign_integrity=False)

    assert _run_verify(
        monkeypatch,
        client,
        raw_token,
        args=_verify_args(account_integrity_key=""),
    ) == {"agent_id": _VERIFY_AGENT, "verified": True, "integrity_checked": False}


def test_verify_token_accepts_a_wildcard_council_membership(monkeypatch):
    raw_token = "protected-candidate-token"  # pragma: allowlist secret
    client = _verify_client(raw_token=raw_token, councils=("*",))

    assert _run_verify(
        monkeypatch,
        client,
        raw_token,
        args=_verify_args(expect_council=["council-prod"]),
    ) == {"agent_id": _VERIFY_AGENT, "verified": True, "integrity_checked": True}


def test_verify_token_tolerates_whitespace_around_council_separators(monkeypatch):
    """AGENT_COUNCILS may be written with spaces; that must not fail validation."""
    raw_token = "protected-candidate-token"  # pragma: allowlist secret
    client = _verify_client(raw_token=raw_token, councils=("council-prod", "council-two"))

    assert _run_verify(
        monkeypatch,
        client,
        raw_token,
        args=_verify_args(expect_council=["council-prod, council-two"]),
    ) == {"agent_id": _VERIFY_AGENT, "verified": True, "integrity_checked": True}


def test_store_unavailable_exit_status_matches_the_deploy_script_constant():
    """The deploy script branches on this number; drift would silently break it."""
    deploy_stack = (Path(__file__).resolve().parents[1] / "scripts/deploy_stack.sh").read_text()
    match = re.search(r"^ACCOUNT_STORE_UNAVAILABLE=(\d+)$", deploy_stack, re.MULTILINE)
    assert match, "ACCOUNT_STORE_UNAVAILABLE is not defined in scripts/deploy_stack.sh"
    assert int(match.group(1)) == agent_accounts.EXIT_STORE_UNAVAILABLE


def test_verify_token_reports_an_unreachable_store_distinctly(monkeypatch):
    """A Redis outage must not be reported as an invalid credential.

    The deploy script keys its rotation guidance off this distinction; conflating
    the two would tell an operator to destroy a working token because Redis
    blipped.
    """
    raw_token = "protected-candidate-token"  # pragma: allowlist secret
    client = _verify_client(raw_token=raw_token, error=ConnectionError("redis is down"))

    with pytest.raises(agent_accounts.AccountStoreUnavailableError):
        _run_verify(monkeypatch, client, raw_token)

    # A store that cannot be opened at all. open_durable_store reports a failed
    # connection as a plain AccountCliError, which exits 1; verify_token has to
    # promote that to unavailable or the deploy reads an outage as a bad token.
    @contextlib.asynccontextmanager
    async def _refuse_to_open(_args):
        raise agent_accounts.AccountCliError("cannot connect to Redis: no route to host")
        yield  # pragma: no cover - unreachable, keeps this a generator

    monkeypatch.delenv("COUNCIL_STORAGE_MODE", raising=False)
    monkeypatch.setattr(agent_accounts, "open_durable_store", _refuse_to_open)
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw_token))
    with pytest.raises(agent_accounts.AccountStoreUnavailableError):
        asyncio.run(agent_accounts.verify_token(_verify_args()))


def test_password_only_redis_urls_are_redacted(monkeypatch):
    """Nomos' Redis URLs carry no username, which the first pattern required.

    redis://:secret@host is the form deploy_stack and the compose wrappers use, so
    a fallback that only matched user:pass@ left the real deployments' passwords
    in any driver error that reached a log.
    """
    monkeypatch.delenv("COUNCIL_REDIS_PASSWORD", raising=False)
    monkeypatch.delenv("NOMOS_REDIS_PASSWORD", raising=False)
    monkeypatch.delenv("REDIS_PASSWORD", raising=False)

    exc = RuntimeError(
        "cannot connect to redis://:sup3rs3cret@redis:6379/0"
    )  # pragma: allowlist secret

    rendered = agent_accounts.redact_secrets(exc)

    assert "sup3rs3cret" not in rendered
    assert "redis:6379" in rendered


def test_redis_url_parts_are_addressable_by_name():
    """Positional indexing into a 5-tuple is how [3] silently became the wrong field."""
    parts = agent_accounts._redis_url_parts(
        "rediss://:pw@example:6380/2"
    )  # pragma: allowlist secret

    assert parts.host == "example"
    assert parts.port == 6380
    assert parts.db == 2
    assert parts.password == "pw"  # pragma: allowlist secret
    assert parts.tls is True


def test_default_redis_url_is_built_without_the_password(monkeypatch):
    """The password must not travel through a URL it would have to be escaped in."""
    monkeypatch.delenv("NOMOS_REDIS_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("COUNCIL_REDIS_HOST", "redis-primary")
    monkeypatch.setenv("COUNCIL_REDIS_PORT", "6380")
    monkeypatch.setenv(
        "COUNCIL_REDIS_PASSWORD",
        "p@ss:word/with#specials",  # pragma: allowlist secret
    )  # pragma: allowlist secret

    url = agent_accounts.default_redis_url()

    assert url == "redis://redis-primary:6380/0"
    assert "p@ss" not in url
    # ...and the password still reaches the config, just not via the URL.
    ns = Namespace(redis_url=url, prefix="nomos:", account_integrity_key="k" * 32)
    assert (
        agent_accounts.redis_config_from_args(ns).password
        == "p@ss:word/with#specials"  # pragma: allowlist secret
    )  # pragma: allowlist secret
