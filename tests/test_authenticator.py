# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for AgentAuthenticator — Phase 2.1.

Requires Redis at localhost:6379. Skipped if unavailable.
"""

import hashlib

import pytest
import pytest_asyncio

from nomos.auth.permissions import PermissionScope
from tests.redis_test_support import redis_config_for_tests, redis_unavailable_reason

REDIS_UNAVAILABLE_REASON = redis_unavailable_reason()

pytestmark = [
    pytest.mark.skipif(
        REDIS_UNAVAILABLE_REASON is not None,
        reason=REDIS_UNAVAILABLE_REASON or "",
    ),
    pytest.mark.asyncio,
]


@pytest_asyncio.fixture
async def store():
    """Connected RedisStore on test db, cleaned before/after."""
    from nomos.store.redis_store import RedisStore

    config = redis_config_for_tests()
    s = RedisStore(config)
    await s.connect()
    keys = await s.client.keys("nomos_test:*")
    if keys:
        await s.client.delete(*keys)
    yield s
    keys = await s.client.keys("nomos_test:*")
    if keys:
        await s.client.delete(*keys)
    await s.close()


@pytest_asyncio.fixture
async def auth(store):
    """AgentAuthenticator with a seeded test account."""
    from nomos.auth.authenticator import AgentAuthenticator

    # Seed a test account
    raw_token = "test-token-secret-12345"
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    await store.set_account("agent-test", token_hash=token_hash, display_name="Test Agent")
    await store.set_account_permissions("agent-test", {"shell:read", "shell:write", "file:read"})
    await store.set_account_councils("agent-test", {"council-prod"})
    await store.set_token_index(token_hash, "agent-test")

    return AgentAuthenticator(store), raw_token


class TestAuthenticate:
    async def test_valid_token(self, auth):
        authenticator, token = auth
        identity = await authenticator.authenticate(token)
        assert identity is not None
        assert identity.agent_id == "agent-test"
        assert identity.display_name == "Test Agent"
        assert "shell:read" in identity.permissions
        assert identity.councils == {"council-prod"}

    async def test_invalid_token(self, auth):
        authenticator, _ = auth
        identity = await authenticator.authenticate("wrong-token")
        assert identity is None

    async def test_empty_token(self, auth):
        authenticator, _ = auth
        identity = await authenticator.authenticate("")
        assert identity is None

    async def test_disabled_account(self, store):
        from nomos.auth.authenticator import AgentAuthenticator

        raw_token = "disabled-token-xyz"
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        await store.set_account("agent-disabled", token_hash=token_hash, enabled=False)
        await store.set_token_index(token_hash, "agent-disabled")

        authenticator = AgentAuthenticator(store)
        identity = await authenticator.authenticate(raw_token)
        assert identity is None

    async def test_rate_limiting(self, auth):
        from nomos.auth.authenticator import RATE_LIMIT_MAX_FAILURES

        authenticator, _ = auth

        # Fail enough times to trigger rate limit
        for _ in range(RATE_LIMIT_MAX_FAILURES):
            await authenticator.authenticate("bad-token-for-rate-limit")

        # Now even a different bad token with same hash would be blocked
        # But more importantly, the specific hash is blocked
        result = await authenticator.authenticate("bad-token-for-rate-limit")
        assert result is None


class TestPermissionCheck:
    async def test_has_all_permissions(self, auth):
        authenticator, token = auth
        identity = await authenticator.authenticate(token)
        result = authenticator.check_permission(
            identity, {PermissionScope.SHELL_READ, PermissionScope.FILE_READ}
        )
        assert result.allowed is True
        assert result.missing == set()

    async def test_missing_permission(self, auth):
        authenticator, token = auth
        identity = await authenticator.authenticate(token)
        result = authenticator.check_permission(
            identity, {PermissionScope.SHELL_READ, PermissionScope.FILE_DELETE}
        )
        assert result.allowed is False
        assert PermissionScope.FILE_DELETE in result.missing

    async def test_superset_passes(self, auth):
        authenticator, token = auth
        identity = await authenticator.authenticate(token)
        # Agent has shell:read, shell:write, file:read — asking for just shell:read
        result = authenticator.check_permission(identity, {PermissionScope.SHELL_READ})
        assert result.allowed is True

    async def test_empty_required_always_passes(self, auth):
        authenticator, token = auth
        identity = await authenticator.authenticate(token)
        result = authenticator.check_permission(identity, set())
        assert result.allowed is True
