# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared Redis client construction for standalone and Sentinel topologies."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import unquote, urlparse

import redis.asyncio as aioredis
from redis.asyncio.sentinel import Sentinel


@dataclass(frozen=True)
class RedisConnectionConfig:
    mode: str = "standalone"
    url: str = ""
    host: str = "localhost"
    port: int = 6379
    db: int = 0
    password: str = ""
    tls: bool = False
    sentinel_service_name: str = ""
    sentinel_nodes: tuple[str, ...] = ()
    sentinel_password: str = ""
    connect_timeout_ms: int = 1000
    command_timeout_ms: int = 1000


def create_redis_client(config: RedisConnectionConfig) -> aioredis.Redis[str]:
    """Create an async Redis client for the configured topology."""
    mode = config.mode.strip().lower()
    kwargs = _connection_kwargs(config)
    if mode == "standalone":
        if config.url:
            return cast(
                "aioredis.Redis[str]",
                aioredis.from_url(config.url, decode_responses=True, **kwargs),
            )
        return cast(
            "aioredis.Redis[str]",
            aioredis.Redis(
                host=config.host,
                port=config.port,
                db=config.db,
                password=config.password or None,
                ssl=config.tls,
                decode_responses=True,
                **kwargs,
            ),
        )
    if mode == "sentinel":
        if not config.sentinel_service_name:
            raise ValueError("Redis Sentinel mode requires sentinel_service_name")
        addresses = sentinel_addresses(config.sentinel_nodes)
        redis_password = config.password or _password_from_url(config.url)
        sentinel_kwargs: dict[str, Any] = {
            "decode_responses": True,
            **kwargs,
        }
        sentinel_password = config.sentinel_password or redis_password
        if sentinel_password:
            sentinel_kwargs["password"] = sentinel_password
        if config.tls:
            sentinel_kwargs["ssl"] = True
        sentinel = Sentinel(
            addresses,
            sentinel_kwargs=sentinel_kwargs,
            db=config.db,
            password=redis_password or None,
            ssl=config.tls,
            decode_responses=True,
            **kwargs,
        )
        return cast("aioredis.Redis[str]", sentinel.master_for(config.sentinel_service_name))
    raise ValueError(f"Unsupported Redis mode {config.mode!r}")


def parse_sentinel_nodes(value: str | Iterable[str]) -> tuple[str, ...]:
    """Normalize comma-separated or iterable Sentinel node declarations."""
    if isinstance(value, str):
        raw_nodes: Iterable[str] = value.split(",")
    else:
        raw_nodes = value
    return tuple(node.strip() for node in raw_nodes if node and node.strip())


def sentinel_addresses(nodes: Iterable[str]) -> list[tuple[str, int]]:
    """Parse Sentinel nodes in host:port form."""
    addresses: list[tuple[str, int]] = []
    for node in parse_sentinel_nodes(nodes):
        host, sep, port_text = node.rpartition(":")
        if not sep or not host or not port_text:
            raise ValueError(f"Redis Sentinel node must be host:port, got {node!r}")
        try:
            port = int(port_text)
        except ValueError as exc:
            raise ValueError(f"Redis Sentinel node has invalid port: {node!r}") from exc
        addresses.append((host, port))
    if not addresses:
        raise ValueError("Redis Sentinel mode requires at least one sentinel node")
    return addresses


def _connection_kwargs(config: RedisConnectionConfig) -> dict[str, Any]:
    return {
        "socket_connect_timeout": max(config.connect_timeout_ms, 1) / 1000,
        "socket_timeout": max(config.command_timeout_ms, 1) / 1000,
        "health_check_interval": 30,
        "socket_keepalive": True,
    }


def _password_from_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    return unquote(parsed.password or "")
