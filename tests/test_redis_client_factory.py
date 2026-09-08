# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest

from nomos_runtime import redis_client
from nomos_runtime.redis_client import (
    RedisConnectionConfig,
    create_redis_client,
    sentinel_addresses,
)
from scripts.generate_dev_mtls_pki import generate


def test_sentinel_addresses_parse_host_port_nodes() -> None:
    assert sentinel_addresses(["redis-sentinel:26379", "redis-sentinel-2:26380"]) == [
        ("redis-sentinel", 26379),
        ("redis-sentinel-2", 26380),
    ]


def test_sentinel_addresses_reject_empty_nodes() -> None:
    with pytest.raises(ValueError, match="at least one"):
        sentinel_addresses([])


def test_create_sentinel_client_uses_master_for(monkeypatch: pytest.MonkeyPatch) -> None:
    created: dict[str, object] = {}

    class FakeSentinel:
        def __init__(self, sentinels, sentinel_kwargs=None, **connection_kwargs):
            created["sentinels"] = sentinels
            created["sentinel_kwargs"] = sentinel_kwargs
            created["connection_kwargs"] = connection_kwargs

        def master_for(self, service_name):
            created["service_name"] = service_name
            return "redis-master-client"

    monkeypatch.setattr(redis_client, "Sentinel", FakeSentinel)

    client = create_redis_client(
        RedisConnectionConfig(
            mode="sentinel",
            url="redis://:redis-password@redis:6379/0",
            sentinel_service_name="nomos-master",
            sentinel_nodes=("redis-sentinel:26379",),
            connect_timeout_ms=250,
            command_timeout_ms=500,
        )
    )

    assert client == "redis-master-client"
    assert created["sentinels"] == [("redis-sentinel", 26379)]
    assert created["service_name"] == "nomos-master"
    assert created["sentinel_kwargs"] == {
        "decode_responses": True,
        "socket_connect_timeout": 0.25,
        "socket_timeout": 0.5,
        "health_check_interval": 30,
        "socket_keepalive": True,
        "password": "redis-password",  # pragma: allowlist secret
    }
    assert created["connection_kwargs"] == {
        "db": 0,
        "password": "redis-password",  # pragma: allowlist secret
        "ssl": False,
        "decode_responses": True,
        "socket_connect_timeout": 0.25,
        "socket_timeout": 0.5,
        "health_check_interval": 30,
        "socket_keepalive": True,
    }


def test_create_sentinel_client_applies_tls_to_discovery_and_master(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    generate(tmp_path)
    created: dict[str, object] = {}

    class FakeSentinel:
        def __init__(self, sentinels, sentinel_kwargs=None, **connection_kwargs):
            created["sentinel_kwargs"] = sentinel_kwargs
            created["connection_kwargs"] = connection_kwargs

        def master_for(self, service_name):
            return "redis-master-client"

    monkeypatch.setattr(redis_client, "Sentinel", FakeSentinel)
    config = RedisConnectionConfig(
        mode="sentinel",
        password="redis-password",  # pragma: allowlist secret
        sentinel_service_name="nomos-master",
        sentinel_nodes=("redis-sentinel:26379",),
        tls=True,
        tls_ca_file=str(tmp_path / "ca.crt"),
        tls_cert_file=str(tmp_path / "council.crt"),
        tls_key_file=str(tmp_path / "council.key"),
    )

    assert create_redis_client(config) == "redis-master-client"
    for kwargs in (created["sentinel_kwargs"], created["connection_kwargs"]):
        assert isinstance(kwargs, dict)
        assert kwargs["ssl_ca_certs"] == str(tmp_path / "ca.crt")
        assert kwargs["ssl_certfile"] == str(tmp_path / "council.crt")
        assert kwargs["ssl_keyfile"] == str(tmp_path / "council.key")
        assert kwargs["ssl_cert_reqs"] == "required"
        assert kwargs["ssl_check_hostname"] is True


def test_redis_tls_rejects_partial_or_disabled_material(tmp_path: Path) -> None:
    generate(tmp_path)
    with pytest.raises(ValueError, match="requires tls_ca_file"):
        create_redis_client(RedisConnectionConfig(tls=True))
    with pytest.raises(ValueError, match="while TLS is disabled"):
        create_redis_client(RedisConnectionConfig(tls_ca_file=str(tmp_path / "ca.crt")))
    with pytest.raises(ValueError, match="requires Redis TLS"):
        create_redis_client(RedisConnectionConfig(url="rediss://redis:6379/0"))
