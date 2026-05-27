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

    response = TestClient(app).post("/api/agents", json={"agent_id": "reader"})

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

    response = TestClient(app).get("/")

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
    )

    assert response.status_code == 403
    assert store == {}


def test_signup_http_endpoint_rejects_admin_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    store: dict[str, Any] = {}
    monkeypatch.setattr(server, "redis_client", lambda _settings: FakeRedis(store))
    app = server.create_app(make_settings())

    response = TestClient(app).post("/api/agents", json={"agent_id": "admin", "role": "admin"})

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
    signup = client.post("/api/agents", json={"agent_id": "cfg-agent"})

    assert signup.status_code == 201
    assert signup.json()["council_id"] == "council-dev"
    assert store["nomos:accounts:cfg-agent:councils"] == {"council-dev"}
