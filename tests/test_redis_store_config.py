# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, cast

import pytest

from nomos.config.settings import RedisConfig
from nomos.store.redis_store import RedisStore


@dataclass(frozen=True)
class _RedisConfigFixture:
    """Constructor-only Redis config that deliberately bypasses model validation."""

    tls: bool = True
    tls_cert_file: str = "/tls/client.crt"
    tls_key_file: str = "/tls/client.key"
    tls_verify_hostname: bool = True
    key_prefix: str = "nomos:"
    account_integrity_key: str = ""


def _config(**overrides: Any) -> RedisConfig:
    return cast(RedisConfig, replace(_RedisConfigFixture(), **overrides))


@pytest.mark.parametrize(
    "missing",
    ["tls", "tls_cert_file", "tls_key_file", "tls_verify_hostname"],
)
def test_strict_profile_rejects_incomplete_redis_mtls(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.setenv("NOMOS_DEPLOY_PROFILE", "production")
    with pytest.raises(ValueError, match="mTLS with hostname verification"):
        RedisStore(_config(**{missing: False if missing in {"tls", "tls_verify_hostname"} else ""}))


def test_strict_profile_accepts_complete_redis_mtls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOMOS_DEPLOY_PROFILE", "production")
    store = RedisStore(_config())
    assert store._client is None


def test_redis_store_rejects_unknown_deployment_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOMOS_DEPLOY_PROFILE", "productoin")
    with pytest.raises(ValueError, match="unknown deployment profile"):
        RedisStore(_config())
