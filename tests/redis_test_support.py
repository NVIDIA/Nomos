# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared Redis test configuration helpers."""

from __future__ import annotations

import os
from pathlib import Path

import redis
from redis.exceptions import RedisError

from nomos.config.settings import RedisConfig

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_ENV = ROOT / "deploy" / ".env"


def redis_config_for_tests() -> RedisConfig:
    """Build the Redis config used by integration tests."""
    return RedisConfig(
        host=os.environ.get("NOMOS_TEST_REDIS_HOST", "127.0.0.1"),
        port=int(os.environ.get("NOMOS_TEST_REDIS_PORT", "6379")),
        db=int(os.environ.get("NOMOS_TEST_REDIS_DB", "15")),
        password=_redis_password(),
        key_prefix=os.environ.get("NOMOS_TEST_REDIS_PREFIX", "nomos_test:"),
    )


def redis_unavailable_reason() -> str | None:
    """Return a pytest skip reason when the configured Redis cannot be used."""
    config = redis_config_for_tests()
    client = redis.Redis(
        host=config.host,
        port=config.port,
        db=config.db,
        password=config.password or None,
        socket_connect_timeout=0.2,
        socket_timeout=0.5,
        decode_responses=True,
    )
    try:
        client.ping()
    except RedisError as exc:
        return f"Redis not usable at {config.host}:{config.port}: {exc.__class__.__name__}"
    finally:
        client.close()
    return None


def _redis_password() -> str:
    if "NOMOS_TEST_REDIS_PASSWORD" in os.environ:
        return os.environ["NOMOS_TEST_REDIS_PASSWORD"]
    return os.environ.get("REDIS_PASSWORD") or _read_deploy_env("REDIS_PASSWORD") or "nomos-dev"


def _read_deploy_env(key: str) -> str:
    if not DEPLOY_ENV.exists():
        return ""
    for line in DEPLOY_ENV.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("export "):
            stripped = stripped.removeprefix("export ").strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        if name.strip() == key:
            return _unquote_env_value(value.strip())
    return ""


def _unquote_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
