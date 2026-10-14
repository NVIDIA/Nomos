# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import ssl
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
    assert created["sentinel_kwargs"]["ssl"] is True
    for kwargs in (created["sentinel_kwargs"], created["connection_kwargs"]):
        assert isinstance(kwargs, dict)
        assert kwargs["ssl_ca_certs"] == str(tmp_path / "ca.crt")
        assert kwargs["ssl_certfile"] == str(tmp_path / "council.crt")
        assert kwargs["ssl_keyfile"] == str(tmp_path / "council.key")
        assert kwargs["ssl_cert_reqs"] == "required"
        assert kwargs["ssl_check_hostname"] is True


@pytest.mark.parametrize("mode", ["standalone", "standalone-url", "sentinel"])
def test_redis_tls_connection_enforces_minimum_version_at_dependency_floor(
    tmp_path: Path, mode: str
) -> None:
    generate(tmp_path)
    config = RedisConnectionConfig(
        mode="sentinel" if mode == "sentinel" else "standalone",
        url="redis://localhost:6379/0" if mode == "standalone-url" else "",
        sentinel_service_name="nomos-master" if mode == "sentinel" else "",
        sentinel_nodes=("localhost:26379",) if mode == "sentinel" else (),
        tls=True,
        tls_ca_file=str(tmp_path / "ca.crt"),
        tls_cert_file=str(tmp_path / "council.crt"),
        tls_key_file=str(tmp_path / "council.key"),
    )
    client = create_redis_client(config)
    connection = client.connection_pool.make_connection()
    assert connection.ssl_context.get().minimum_version == ssl.TLSVersion.TLSv1_2


def test_redis_tls_rejects_partial_or_disabled_material(tmp_path: Path) -> None:
    generate(tmp_path)
    with pytest.raises(ValueError, match="requires tls_ca_file"):
        create_redis_client(RedisConnectionConfig(tls=True))
    with pytest.raises(ValueError, match="while TLS is disabled"):
        create_redis_client(RedisConnectionConfig(tls_ca_file=str(tmp_path / "ca.crt")))
    with pytest.raises(ValueError, match="requires Redis TLS"):
        create_redis_client(RedisConnectionConfig(url="rediss://redis:6379/0"))
    with pytest.raises(ValueError, match="requires Redis TLS"):
        create_redis_client(
            RedisConnectionConfig(
                mode="sentinel",
                url="rediss://redis:6379/0",
                sentinel_service_name="nomos-master",
                sentinel_nodes=("redis-sentinel:26379",),
            )
        )


def test_redis_url_cannot_override_verified_tls(tmp_path: Path) -> None:
    generate(tmp_path)
    config = RedisConnectionConfig(
        url="rediss://redis:6379/0?ssl_cert_reqs=none&ssl_check_hostname=false",
        tls=True,
        tls_ca_file=str(tmp_path / "ca.crt"),
        tls_cert_file=str(tmp_path / "council.crt"),
        tls_key_file=str(tmp_path / "council.key"),
    )
    with pytest.raises(ValueError, match="cannot override validated TLS settings"):
        create_redis_client(config)


def test_redis_tls_minimum_version_reaches_connection_pool(tmp_path: Path) -> None:
    generate(tmp_path)
    client = create_redis_client(
        RedisConnectionConfig(
            url="rediss://redis:6379/0",
            tls=True,
            tls_ca_file=str(tmp_path / "ca.crt"),
            tls_cert_file=str(tmp_path / "council.crt"),
            tls_key_file=str(tmp_path / "council.key"),
            tls_minimum_version="TLSv1.3",
        )
    )
    assert client.connection_pool.connection_kwargs["ssl_min_version"] is ssl.TLSVersion.TLSv1_3
