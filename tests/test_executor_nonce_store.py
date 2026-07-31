# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from executors.docker import nonce_store as nonce_store_module
from executors.docker.nonce_store import NonceStore


class FakeRedisClient:
    def __init__(self, set_results=None) -> None:
        self.set_results = list(set_results or [])
        self.closed = False
        self.set_calls = []

    async def ping(self):
        return True

    async def aclose(self):
        self.closed = True

    async def set(self, key, value, *, nx, ex):
        self.set_calls.append((key, value, nx, ex))
        result = self.set_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.mark.asyncio
async def test_claim_nonce_reconnects_once_after_closed_connection(monkeypatch):
    stale_client = FakeRedisClient([RedisConnectionError("Connection closed by server.")])
    fresh_client = FakeRedisClient([True])
    clients = [stale_client, fresh_client]
    from_url_calls = []

    def fake_from_url(redis_url, **kwargs):
        from_url_calls.append((redis_url, kwargs))
        return clients.pop(0)

    monkeypatch.setattr(nonce_store_module.aioredis, "from_url", fake_from_url)

    store = NonceStore("redis://:secret@redis:6379/0")
    await store.connect()

    assert await store.claim_nonce("nonce-1", "writ-1", 60) is True
    assert stale_client.closed is True
    assert fresh_client.set_calls == [("nomos:nonces:nonce-1", "writ-1", True, 60)]
    assert from_url_calls[0][1]["health_check_interval"] == 30
    assert from_url_calls[0][1]["socket_keepalive"] is True
