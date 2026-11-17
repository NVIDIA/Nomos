# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import base64
import hashlib
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_signup import server
from nomos_runtime import DeploymentPreflightError


class FakePipeline:
    def __init__(self, store: dict[str, Any]) -> None:
        self.store = store
        self.ops: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def hset(self, key: str, *, mapping: dict[str, str]) -> FakePipeline:
        self.ops.append(("hset", (key,), {"mapping": mapping}))
        return self

    def hdel(self, key: str, *fields: str) -> FakePipeline:
        self.ops.append(("hdel", (key, *fields), {}))
        return self

    def sadd(self, key: str, *values: str) -> FakePipeline:
        self.ops.append(("sadd", (key, *values), {}))
        return self

    def set(self, key: str, value: str) -> FakePipeline:
        self.ops.append(("set", (key, value), {}))
        return self

    def delete(self, *keys: str) -> FakePipeline:
        self.ops.append(("delete", keys, {}))
        return self

    def srem(self, key: str, *values: str) -> FakePipeline:
        self.ops.append(("srem", (key, *values), {}))
        return self

    async def execute(self) -> None:
        for op, args, kwargs in self.ops:
            if op == "hset":
                self.store.setdefault(args[0], {}).update(kwargs["mapping"])
            elif op == "hdel":
                key, *fields = args
                existing = self.store.get(key)
                if isinstance(existing, dict):
                    for field in fields:
                        existing.pop(field, None)
            elif op == "sadd":
                key, *values = args
                self.store.setdefault(key, set())
                self.store[key].update(values)
            elif op == "set":
                key, value = args
                self.store[key] = value
            elif op == "delete":
                for key in args:
                    self.store.pop(key, None)
            elif op == "srem":
                key, *values = args
                existing = self.store.get(key)
                if isinstance(existing, set):
                    existing.difference_update(values)


class FakeRedis:
    def __init__(self, store: dict[str, Any]) -> None:
        self.store = store
        self.closed = False

    async def ping(self) -> bool:
        return True

    async def exists(self, key: str) -> int:
        return int(key in self.store)

    async def hgetall(self, key: str) -> dict[str, str]:
        value = self.store.get(key, {})
        return dict(value) if isinstance(value, dict) else {}

    async def hset(self, key: str, *, mapping: dict[str, str]) -> None:
        self.store.setdefault(key, {}).update(mapping)

    async def hdel(self, key: str, *fields: str) -> None:
        existing = self.store.get(key)
        if isinstance(existing, dict):
            for field in fields:
                existing.pop(field, None)

    async def smembers(self, key: str) -> set[str]:
        value = self.store.get(key, set())
        return set(value) if isinstance(value, set) else set()

    async def scan_iter(self, match: str):
        pattern = re.compile("^" + re.escape(match).replace("\\*", ".*") + "$")
        for key in sorted(self.store):
            if pattern.fullmatch(key):
                yield key

    def pipeline(self) -> FakePipeline:
        return FakePipeline(self.store)

    async def aclose(self) -> None:
        self.closed = True


def make_settings(**overrides: Any) -> server.SignupSettings:
    values = {
        "redis_url": "redis://example",
        "redis_prefix": "nomos:",
        "default_council_id": "council-prod",
        "allowed_councils": ("council-prod",),
        "default_role": "developer",
        "allowed_roles": ("readonly", "developer"),
        "council_endpoint": "http://localhost:9901/mcp",
        "admin_user": "admin",
        "admin_password": "test-admin-password",  # pragma: allowlist secret
    }
    values.update(overrides)
    return server.SignupSettings(**values)


def test_settings_from_env_reads_sentinel_topology(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOMOS_SIGNUP_REDIS_MODE", "sentinel")
    monkeypatch.setenv("NOMOS_SIGNUP_REDIS_SENTINEL_SERVICE_NAME", "nomos-master")
    monkeypatch.setenv(
        "NOMOS_SIGNUP_REDIS_SENTINEL_NODES",
        "sentinel-a:26379,sentinel-b:26379",
    )
    monkeypatch.setenv("REDIS_PASSWORD", "redis-test")  # pragma: allowlist secret

    settings = server.settings_from_env()

    assert settings.redis_mode == "sentinel"
    assert settings.redis_sentinel_service_name == "nomos-master"
    assert settings.redis_sentinel_nodes == ("sentinel-a:26379", "sentinel-b:26379")
    assert settings.redis_password == "redis-test"  # pragma: allowlist secret


def admin_auth() -> dict[str, str]:
    token = base64.b64encode(b"admin:test-admin-password").decode()
    return {"Authorization": f"Basic {token}"}


def test_signup_request_validates_agent_id() -> None:
    with pytest.raises(ValueError, match="agent_id"):
        server.SignupRequest(agent_id="../bad")


def test_signup_request_rejects_unknown_role() -> None:
    with pytest.raises(ValueError, match="role"):
        server.SignupRequest(agent_id="alice", role="superuser")


def test_settings_reject_default_role_outside_allow_list() -> None:
    settings = make_settings(default_role="admin")

    with pytest.raises(ValueError, match="default_role"):
        server.create_app(settings)


def test_settings_dev_profile_allows_current_admin_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NOMOS_DEPLOY_PROFILE", raising=False)

    app = server.create_app(make_settings(admin_password="nomos-dev-admin"))

    assert app.title == "Nomos Agent Signup"


def test_settings_alpha_profile_rejects_default_admin_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOMOS_DEPLOY_PROFILE", "alpha")

    with pytest.raises(DeploymentPreflightError) as exc:
        server.create_app(
            make_settings(
                admin_password="nomos-dev-admin",  # pragma: allowlist secret
                account_integrity_key="generated-account-integrity-key",
            )
        )

    assert exc.value.issues == ("NOMOS_SIGNUP_ADMIN_PASSWORD uses a known development default",)


def test_settings_alpha_profile_requires_account_integrity_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOMOS_DEPLOY_PROFILE", "alpha")

    with pytest.raises(DeploymentPreflightError) as exc:
        server.create_app(
            make_settings(admin_password="generated-admin-password")  # pragma: allowlist secret
        )

    assert exc.value.issues == ("NOMOS_ACCOUNT_INTEGRITY_KEY is required",)


def test_redis_client_strict_profile_requires_hostname_verification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    ca_file = tmp_path / "ca.crt"
    cert_file = tmp_path / "client.crt"
    key_file = tmp_path / "client.key"
    for path in (ca_file, cert_file, key_file):
        path.write_text("test")
    monkeypatch.setenv("NOMOS_DEPLOY_PROFILE", "production")
    monkeypatch.setenv("NOMOS_SIGNUP_REDIS_TLS_ENABLED", "true")
    monkeypatch.setenv("NOMOS_SIGNUP_REDIS_TLS_CA_FILE", str(ca_file))
    monkeypatch.setenv("NOMOS_SIGNUP_REDIS_TLS_CERT_FILE", str(cert_file))
    monkeypatch.setenv("NOMOS_SIGNUP_REDIS_TLS_KEY_FILE", str(key_file))
    monkeypatch.setenv("NOMOS_SIGNUP_REDIS_TLS_VERIFY_HOSTNAME", "false")

    with pytest.raises(ValueError, match="mTLS with hostname verification"):
        server.redis_client(make_settings())


@pytest.mark.asyncio
async def test_create_agent_account_writes_nomos_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    store: dict[str, Any] = {}
    monkeypatch.setattr(server, "redis_client", lambda _settings: FakeRedis(store))
    monkeypatch.setattr(server.secrets, "token_hex", lambda _size: "abc123")

    settings = make_settings()

    response = await server.create_agent_account(
        settings,
        server.SignupRequest(agent_id="alice-agent", display_name="Alice"),
    )

    expected_hash = hashlib.sha256(b"abc123").hexdigest()
    assert response.agent_id == "alice-agent"
    assert response.auth_token == "abc123"
    assert response.council_endpoint == "http://localhost:9901/mcp"
    assert store["nomos:accounts:alice-agent"]["token_hash"] == expected_hash
    assert "auth_token" not in store["nomos:accounts:alice-agent"]
    assert store["nomos:accounts:alice-agent"]["display_name"] == "Alice"
    assert "shell:write" in store["nomos:accounts:alice-agent:perms"]
    assert store["nomos:accounts:alice-agent:councils"] == {"council-prod"}
    assert store["nomos:councils:council-prod:agents"] == {"alice-agent"}
    assert store[f"nomos:token_index:{expected_hash}"] == "alice-agent"


@pytest.mark.asyncio
async def test_create_agent_account_rejects_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    store: dict[str, Any] = {"nomos:accounts:alice-agent": {"enabled": "1"}}
    monkeypatch.setattr(server, "redis_client", lambda _settings: FakeRedis(store))

    settings = make_settings()

    with pytest.raises(server.HTTPException) as exc:
        await server.create_agent_account(settings, server.SignupRequest(agent_id="alice-agent"))

    assert exc.value.status_code == 409


def test_signup_http_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    store: dict[str, Any] = {}
    monkeypatch.setattr(server, "redis_client", lambda _settings: FakeRedis(store))
    monkeypatch.setattr(server.secrets, "token_hex", lambda _size: "def456")
    app = server.create_app(make_settings(default_role="readonly"))

    response = TestClient(app).post(
        "/api/agents", json={"agent_id": "reader"}, headers=admin_auth()
    )

    assert response.status_code == 201
    body = response.json()
    assert body["agent_id"] == "reader"
    assert body["auth_token"] == "def456"
    assert body["role"] == "readonly"
    assert body["permissions"] == ["shell:read", "file:read"]


def test_signup_page_renders_council_choices_and_plugin_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store: dict[str, Any] = {}
    monkeypatch.setattr(server, "redis_client", lambda _settings: FakeRedis(store))
    app = server.create_app(make_settings(allowed_councils=("council-prod", "council-dev")))

    response = TestClient(app).get("/", headers=admin_auth())

    assert response.status_code == 200
    assert '<option value="council-prod" selected>' in response.text
    assert '<option value="council-dev">' in response.text
    assert "plugins/scripts/install.sh council-gate --non-interactive" in response.text
    assert "--backend ${data.council_endpoint}" in response.text


def test_signup_http_endpoint_rejects_disallowed_council(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store: dict[str, Any] = {}
    monkeypatch.setattr(server, "redis_client", lambda _settings: FakeRedis(store))
    app = server.create_app(make_settings())

    response = TestClient(app).post(
        "/api/agents",
        json={"agent_id": "reader", "council_id": "council-dev"},
        headers=admin_auth(),
    )

    assert response.status_code == 403
    assert store == {}


def test_signup_http_endpoint_rejects_admin_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    store: dict[str, Any] = {}
    monkeypatch.setattr(server, "redis_client", lambda _settings: FakeRedis(store))
    app = server.create_app(make_settings())

    response = TestClient(app).post(
        "/api/agents", json={"agent_id": "admin", "role": "admin"}, headers=admin_auth()
    )

    assert response.status_code == 403
    assert store == {}


def test_signup_http_endpoint_rejects_operator_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store: dict[str, Any] = {}
    monkeypatch.setattr(server, "redis_client", lambda _settings: FakeRedis(store))
    app = server.create_app(make_settings())

    response = TestClient(app).post(
        "/api/agents",
        json={"agent_id": "operator", "role": "operator"},
        headers=admin_auth(),
    )

    assert response.status_code == 403
    assert store == {}


def test_signup_http_endpoint_allows_explicit_developer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store: dict[str, Any] = {}
    monkeypatch.setattr(server, "redis_client", lambda _settings: FakeRedis(store))
    monkeypatch.setattr(server.secrets, "token_hex", lambda _size: "dev-token")
    app = server.create_app(make_settings(default_role="readonly"))

    response = TestClient(app).post(
        "/api/agents",
        json={"agent_id": "developer", "role": "developer"},
        headers=admin_auth(),
    )

    assert response.status_code == 201
    assert response.json()["role"] == "developer"


def test_healthz_returns_503_when_redis_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    class DownRedis:
        async def ping(self) -> bool:
            raise ConnectionError("down")

        async def aclose(self) -> None:
            pass

    app = server.create_app(make_settings())
    monkeypatch.setattr(server, "redis_client", lambda _settings: DownRedis())
    response = TestClient(app).get("/healthz")

    assert response.status_code == 503
    assert response.json() == {"ok": False, "redis": "unreachable"}


def test_admin_accounts_requires_basic_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    store: dict[str, Any] = {}
    monkeypatch.setattr(server, "redis_client", lambda _settings: FakeRedis(store))
    app = server.create_app(make_settings())

    response = TestClient(app).get("/api/admin/accounts")

    assert response.status_code == 401


def test_admin_accounts_lists_records(monkeypatch: pytest.MonkeyPatch) -> None:
    token_hash = hashlib.sha256(b"token").hexdigest()
    store: dict[str, Any] = {
        "nomos:accounts:alice-agent": {
            "token_hash": token_hash,
            "display_name": "Alice",
            "created_at": "1",
            "updated_at": "2",
            "enabled": "1",
            "role": "developer",
            "auth_token": "legacy-raw-token",
        },
        "nomos:accounts:alice-agent:perms": {"shell:read", "file:read"},
        "nomos:accounts:alice-agent:councils": {"council-prod", "council-dev"},
    }
    monkeypatch.setattr(server, "redis_client", lambda _settings: FakeRedis(store))
    app = server.create_app(make_settings())

    response = TestClient(app).get("/api/admin/accounts", headers=admin_auth())

    assert response.status_code == 200
    body = response.json()
    assert body["accounts"][0]["agent_id"] == "alice-agent"
    assert body["accounts"][0]["display_name"] == "Alice"
    assert body["accounts"][0]["councils"] == ["council-dev", "council-prod"]
    assert body["accounts"][0]["token_hash_prefix"] == token_hash[:12]
    assert body["accounts"][0]["auth_token"] == ""
    assert "auth_token" not in store["nomos:accounts:alice-agent"]


def test_admin_account_patch_updates_metadata_and_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_hash = hashlib.sha256(b"token").hexdigest()
    store: dict[str, Any] = {
        "nomos:accounts:alice-agent": {
            "token_hash": token_hash,
            "display_name": "Alice",
            "created_at": "1",
            "updated_at": "2",
            "enabled": "1",
            "role": "developer",
        },
        "nomos:accounts:alice-agent:perms": {"shell:read", "shell:write"},
        "nomos:accounts:alice-agent:councils": {"council-prod", "council-old"},
        "nomos:councils:council-prod:agents": {"alice-agent"},
        "nomos:councils:council-old:agents": {"alice-agent"},
    }
    monkeypatch.setattr(server, "redis_client", lambda _settings: FakeRedis(store))
    app = server.create_app(make_settings(allowed_councils=("council-prod", "council-new")))

    response = TestClient(app).patch(
        "/api/admin/accounts/alice-agent",
        headers=admin_auth(),
        json={
            "display_name": "Alice Updated",
            "enabled": False,
            "role": "readonly",
            "councils": ["council-prod", "council-new"],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["display_name"] == "Alice Updated"
    assert body["enabled"] is False
    assert body["role"] == "readonly"
    assert body["permissions"] == ["file:read", "shell:read"]
    assert body["councils"] == ["council-new", "council-prod"]
    assert store["nomos:accounts:alice-agent"]["enabled"] == "0"
    assert store["nomos:accounts:alice-agent:perms"] == {"shell:read", "file:read"}
    assert store["nomos:accounts:alice-agent:councils"] == {"council-prod", "council-new"}
    assert store["nomos:councils:council-old:agents"] == set()
    assert store["nomos:councils:council-new:agents"] == {"alice-agent"}


def test_admin_rotate_token_returns_one_time_token_and_updates_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_hash = hashlib.sha256(b"old").hexdigest()
    new_hash = hashlib.sha256(b"new-token").hexdigest()
    store: dict[str, Any] = {
        "nomos:accounts:alice-agent": {
            "token_hash": old_hash,
            "auth_token": "old",
            "display_name": "Alice",
            "created_at": "1",
            "updated_at": "2",
            "enabled": "1",
            "role": "developer",
        },
        "nomos:accounts:alice-agent:perms": {"shell:read"},
        "nomos:accounts:alice-agent:councils": {"council-prod"},
        f"nomos:token_index:{old_hash}": "alice-agent",
    }
    monkeypatch.setattr(server, "redis_client", lambda _settings: FakeRedis(store))
    monkeypatch.setattr(server.secrets, "token_hex", lambda _size: "new-token")
    app = server.create_app(make_settings())

    response = TestClient(app).post(
        "/api/admin/accounts/alice-agent/rotate-token",
        headers=admin_auth(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["auth_token"] == "new-token"
    assert body["account"]["token_hash_prefix"] == new_hash[:12]
    assert body["account"]["auth_token"] == ""
    assert "auth_token" not in store["nomos:accounts:alice-agent"]
    assert f"nomos:token_index:{old_hash}" not in store
    assert store[f"nomos:token_index:{new_hash}"] == "alice-agent"


def test_admin_delete_removes_account_indexes(monkeypatch: pytest.MonkeyPatch) -> None:
    token_hash = hashlib.sha256(b"token").hexdigest()
    store: dict[str, Any] = {
        "nomos:accounts:alice-agent": {
            "token_hash": token_hash,
            "display_name": "Alice",
            "created_at": "1",
            "updated_at": "2",
            "enabled": "1",
            "role": "developer",
        },
        "nomos:accounts:alice-agent:perms": {"shell:read"},
        "nomos:accounts:alice-agent:councils": {"council-prod"},
        "nomos:councils:council-prod:agents": {"alice-agent"},
        f"nomos:token_index:{token_hash}": "alice-agent",
    }
    monkeypatch.setattr(server, "redis_client", lambda _settings: FakeRedis(store))
    app = server.create_app(make_settings())

    response = TestClient(app).delete("/api/admin/accounts/alice-agent", headers=admin_auth())

    assert response.status_code == 204
    assert "nomos:accounts:alice-agent" not in store
    assert "nomos:accounts:alice-agent:perms" not in store
    assert "nomos:accounts:alice-agent:councils" not in store
    assert f"nomos:token_index:{token_hash}" not in store
    assert store["nomos:councils:council-prod:agents"] == set()


def test_admin_config_roundtrip_limits_public_signup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store: dict[str, Any] = {}
    monkeypatch.setattr(server, "redis_client", lambda _settings: FakeRedis(store))
    app = server.create_app(make_settings())
    client = TestClient(app)

    response = client.patch(
        "/api/admin/config",
        headers=admin_auth(),
        json={
            "default_council_id": "council-dev",
            "allowed_councils": ["council-prod", "council-dev"],
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "default_council_id": "council-dev",
        "allowed_councils": ["council-prod", "council-dev"],
    }
    monkeypatch.setattr(server.secrets, "token_hex", lambda _size: "cfg-token")
    signup = client.post("/api/agents", json={"agent_id": "cfg-agent"}, headers=admin_auth())

    assert signup.status_code == 201
    assert signup.json()["council_id"] == "council-dev"
    assert store["nomos:accounts:cfg-agent:councils"] == {"council-dev"}


def test_create_app_supports_postgres_storage_mode(monkeypatch):
    monkeypatch.setenv("COUNCIL_STORAGE_MODE", "postgres")
    monkeypatch.setenv("COUNCIL_DATABASE_URL", "postgresql://localhost/nomos")
    monkeypatch.setenv("NOMOS_ACCOUNT_INTEGRITY_KEY", "integrity-key")
    monkeypatch.setenv("NOMOS_SIGNUP_ADMIN_PASSWORD", "signup-pass")  # pragma: allowlist secret

    from agent_signup.server import create_app

    app = create_app()

    assert app.title == "Nomos Agent Signup"


def test_create_app_postgres_requires_database_url(monkeypatch):
    with pytest.raises(ValueError, match="COUNCIL_DATABASE_URL is required"):
        server.create_app(make_settings(storage_mode="postgres", account_integrity_key="k"))


def test_create_app_postgres_requires_integrity_key(monkeypatch):
    with pytest.raises(ValueError, match="NOMOS_ACCOUNT_INTEGRITY_KEY is required"):
        server.create_app(make_settings(storage_mode="postgres", database_url="postgresql://x"))


class FakePostgresStore:
    """In-memory durable store mirroring the PostgresStore methods the console uses."""

    def __init__(self) -> None:
        self.accounts: dict[str, dict[str, str]] = {}
        self.permissions: dict[str, set[str]] = {}
        self.councils: dict[str, set[str]] = {}
        self.token_index: dict[str, str] = {}
        self.trusted_executors: dict[str, dict[str, str]] = {}

    async def get_account(self, agent_id: str) -> dict[str, str] | None:
        return self.accounts.get(agent_id)

    async def set_account(
        self, agent_id: str, token_hash: str, display_name: str = "", enabled: bool = True
    ) -> None:
        self.accounts[agent_id] = {
            "token_hash": token_hash,
            "display_name": display_name or agent_id,
            "enabled": "1" if enabled else "0",
        }

    async def set_token_index(self, token_hash: str, agent_id: str) -> None:
        self.token_index[token_hash] = agent_id

    async def set_account_permissions(self, agent_id: str, permissions: set[str]) -> None:
        self.permissions[agent_id] = set(permissions)

    async def get_account_permissions(self, agent_id: str) -> set[str]:
        return set(self.permissions.get(agent_id, set()))

    async def grant_account_council(self, agent_id: str, council_id: str) -> None:
        self.councils.setdefault(agent_id, set()).add(council_id)

    # The console now writes through nomos.auth.provisioning, which uses more of
    # the DurableStore surface than the console's old inline sequence did.
    async def set_account_councils(self, agent_id: str, councils: set[str]) -> None:
        self.councils[agent_id] = set(councils)

    async def update_account_fields(self, agent_id: str, fields: dict[str, str]) -> None:
        self.accounts.setdefault(agent_id, {}).update(fields)

    async def delete_token_index(self, token_hash: str) -> None:
        self.token_index.pop(token_hash, None)

    async def get_account_councils(self, agent_id: str) -> set[str]:
        return set(self.councils.get(agent_id, set()))

    def verify_account_integrity(self, agent_id, account, permissions, councils) -> bool:
        return True

    async def register_trusted_executor(
        self, *, executor_id, public_key_hex, key_fingerprint, metadata=None, enabled=True
    ) -> None:
        self.trusted_executors[key_fingerprint] = {
            "executor_id": executor_id,
            "public_key_hex": public_key_hex,
            "key_fingerprint": key_fingerprint,
            "enabled": "1" if enabled else "0",
        }

    async def get_trusted_executor(self, key_fingerprint: str) -> dict[str, str] | None:
        return self.trusted_executors.get(key_fingerprint)

    async def health_check(self) -> bool:
        return True


def _pg_app(store: FakePostgresStore, **overrides):
    settings = make_settings(
        storage_mode="postgres",
        database_url="postgresql://localhost/nomos",
        account_integrity_key="integrity-key",
        **overrides,
    )
    return server.create_app(settings, account_store=store)


def test_pg_create_agent_writes_account_and_token_chain(monkeypatch):
    monkeypatch.setattr(server.secrets, "token_hex", lambda _size: "pg-token")
    store = FakePostgresStore()
    app = _pg_app(store)

    response = TestClient(app).post(
        "/api/agents", json={"agent_id": "alice-agent"}, headers=admin_auth()
    )

    assert response.status_code == 201
    body = response.json()
    expected_hash = hashlib.sha256(b"pg-token").hexdigest()
    assert body["auth_token"] == "pg-token"
    assert body["te_executor_id"] == ""
    # token chain the council's authenticator resolves: token -> hash -> agent
    assert store.accounts["alice-agent"]["token_hash"] == expected_hash
    assert store.token_index[expected_hash] == "alice-agent"
    assert store.permissions["alice-agent"] == {
        "shell:read",
        "shell:write",
        "file:read",
        "file:write",
    }
    assert store.councils["alice-agent"] == {"council-prod"}


def test_pg_create_agent_rejects_duplicate(monkeypatch):
    store = FakePostgresStore()
    store.accounts["alice-agent"] = {"token_hash": "x", "display_name": "Alice", "enabled": "1"}
    app = _pg_app(store)

    response = TestClient(app).post(
        "/api/agents", json={"agent_id": "alice-agent"}, headers=admin_auth()
    )

    assert response.status_code == 409


def test_pg_create_agent_te_paste_registers_pubkey_only(monkeypatch):
    store = FakePostgresStore()
    app = _pg_app(store)
    pubkey = "11" * 32
    fingerprint = hashlib.sha256(bytes.fromhex(pubkey)).hexdigest()

    response = TestClient(app).post(
        "/api/agents",
        json={
            "agent_id": "alice-agent",
            "has_trusted_executor": True,
            "te_mode": "paste",
            "te_public_key_hex": pubkey,
        },
        headers=admin_auth(),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["te_key_fingerprint"] == fingerprint
    assert body["te_executor_id"] == f"agent:{fingerprint[:16]}"
    assert body["te_public_key_hex"] == pubkey
    assert body["te_private_key_hex"] == ""  # paste never yields a private key
    assert store.trusted_executors[fingerprint]["public_key_hex"] == pubkey


def test_pg_create_agent_te_generate_returns_private_key_once(monkeypatch):
    store = FakePostgresStore()
    app = _pg_app(store)

    response = TestClient(app).post(
        "/api/agents",
        json={"agent_id": "alice-agent", "has_trusted_executor": True, "te_mode": "generate"},
        headers=admin_auth(),
    )

    assert response.status_code == 201
    body = response.json()
    assert len(body["te_private_key_hex"]) == 64  # raw ed25519 private key, hex
    assert len(body["te_public_key_hex"]) == 64
    fingerprint = body["te_key_fingerprint"]
    # the store only ever sees the public key, never the private key
    stored = store.trusted_executors[fingerprint]
    assert stored["public_key_hex"] == body["te_public_key_hex"]
    assert not any("private" in key for key in stored)


def test_pg_create_agent_te_paste_rejects_bad_key(monkeypatch):
    store = FakePostgresStore()
    app = _pg_app(store)

    response = TestClient(app).post(
        "/api/agents",
        json={
            "agent_id": "alice-agent",
            "has_trusted_executor": True,
            "te_mode": "paste",
            "te_public_key_hex": "not-hex",
        },
        headers=admin_auth(),
    )

    assert response.status_code == 422


def test_pg_admin_routes_gated_off(monkeypatch):
    store = FakePostgresStore()
    app = _pg_app(store)

    response = TestClient(app).get("/api/admin/accounts", headers=admin_auth())

    assert response.status_code == 501


@pytest.mark.asyncio
async def test_trusted_executor_failure_rolls_the_account_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure after the account is published must not strand a live credential.

    The account and its token index are committed before trusted-executor
    registration runs, so from that point the account authenticates while its raw
    token exists only in the response being abandoned. Without rollback the result
    is an enabled account nobody holds a credential for -- the fail-open shape
    this branch removes elsewhere.
    """
    store: dict[str, Any] = {}
    monkeypatch.setattr(server, "redis_client", lambda _settings: FakeRedis(store))

    async def boom(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        raise RuntimeError("executor key registry unavailable")

    monkeypatch.setattr(server, "_resolve_and_register_te", boom)

    request = server.SignupRequest(
        agent_id="alice-agent",
        display_name="Alice",
        has_trusted_executor=True,
        te_mode="paste",
        te_public_key_hex="ab" * 32,
        te_executor_id="openshell:2e363bc682be3b5e",
    )

    with pytest.raises(server.HTTPException) as exc:
        await server.create_agent_account(make_settings(), request)

    assert exc.value.status_code == 503
    assert "executor key registry" not in str(exc.value.detail)

    leftover = [k for k in store if "alice-agent" in k or "token_index" in k]
    assert not leftover, f"account survived a failed signup: {leftover}"


@pytest.mark.asyncio
async def test_provisioning_failure_does_not_leak_internal_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provisioning messages describe internal state and must not reach the client."""

    async def boom(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise server.provisioning.ProvisioningError(
            "no account integrity key is configured: accounts will be written unsigned"
        )

    monkeypatch.setattr(server.provisioning, "create_account", boom)

    with pytest.raises(server.HTTPException) as exc:
        await server.create_agent_account_pg(
            FakePostgresStore(), make_settings(), server.SignupRequest(agent_id="alice-agent")
        )

    assert exc.value.status_code == 500
    assert "integrity key" not in str(exc.value.detail)
    assert exc.value.detail == "could not create agent account"


@pytest.mark.parametrize("profile", ["alpha", "production", "prod"])
@pytest.mark.parametrize(
    "query",
    [
        "sslmode=require",
        "sslmode=verify-full&sslrootcert=ca.crt&sslcert=client.crt",
        "sslmode=verify-full&sslrootcert=ca.crt&sslkey=client.key",
        "sslmode=verify-full&sslcert=client.crt&sslkey=client.key",
    ],
)
def test_signup_rejects_incomplete_postgres_tls(monkeypatch, profile, query):
    monkeypatch.setenv("NOMOS_DEPLOY_PROFILE", profile)
    with pytest.raises(
        ValueError, match="sslmode=verify-full plus sslrootcert, sslcert, and sslkey"
    ):
        server.create_app(
            make_settings(
                storage_mode="postgres",
                database_url="postgresql://localhost/nomos?" + query,
                account_integrity_key="integrity-key",
            )
        )
