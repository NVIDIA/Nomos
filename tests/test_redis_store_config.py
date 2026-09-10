# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nomos.store.redis_store import RedisStore


def _config(**overrides):
    values = {
        "tls": True,
        "tls_cert_file": "/tls/client.crt",
        "tls_key_file": "/tls/client.key",
        "tls_verify_hostname": True,
        "key_prefix": "nomos:",
        "account_integrity_key": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


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
