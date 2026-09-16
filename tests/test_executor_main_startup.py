# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import urllib.error
from pathlib import Path

import pytest

from executors.docker import main as docker_main
from nomos_runtime import TLSConfigurationError


class FakeNonceStore:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.connect_calls = 0
        self.close_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1
        if self.connect_calls <= self.failures:
            raise OSError("redis unavailable")

    async def close(self) -> None:
        self.close_calls += 1


def test_env_parsers_use_defaults_for_invalid_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXECUTOR_REDIS_CONNECT_RETRIES", "not-an-int")
    monkeypatch.setenv("EXECUTOR_REDIS_CONNECT_RETRY_DELAY", "not-a-float")

    assert docker_main._env_int("EXECUTOR_REDIS_CONNECT_RETRIES", 30) == 30
    assert docker_main._env_float("EXECUTOR_REDIS_CONNECT_RETRY_DELAY", 2.0) == 2.0


def test_env_parsers_clamp_minimum_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXECUTOR_REDIS_CONNECT_RETRIES", "0")
    monkeypatch.setenv("EXECUTOR_REDIS_CONNECT_RETRY_DELAY", "0")

    assert docker_main._env_int("EXECUTOR_REDIS_CONNECT_RETRIES", 30) == 1
    assert docker_main._env_float("EXECUTOR_REDIS_CONNECT_RETRY_DELAY", 2.0) == 0.1


def test_connect_nonce_store_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXECUTOR_REDIS_CONNECT_RETRIES", "3")
    monkeypatch.setenv("EXECUTOR_REDIS_CONNECT_RETRY_DELAY", "0.1")
    sleeps: list[float] = []
    monkeypatch.setattr(docker_main.time, "sleep", sleeps.append)
    nonce_store = FakeNonceStore(failures=2)
    loop = asyncio.new_event_loop()

    try:
        docker_main.connect_nonce_store_with_retries(loop, nonce_store)  # type: ignore[arg-type]
    finally:
        loop.close()

    assert nonce_store.connect_calls == 3
    assert nonce_store.close_calls == 2
    assert sleeps == [0.1, 0.1]


def test_connect_nonce_store_raises_after_retry_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXECUTOR_REDIS_CONNECT_RETRIES", "2")
    monkeypatch.setenv("EXECUTOR_REDIS_CONNECT_RETRY_DELAY", "0.1")
    monkeypatch.setattr(docker_main.time, "sleep", lambda _delay: None)
    nonce_store = FakeNonceStore(failures=2)
    loop = asyncio.new_event_loop()

    try:
        with pytest.raises(RuntimeError, match="Failed to connect to Redis after 2 attempts"):
            docker_main.connect_nonce_store_with_retries(loop, nonce_store)  # type: ignore[arg-type]
    finally:
        loop.close()

    assert nonce_store.connect_calls == 2
    assert nonce_store.close_calls == 2


def test_council_key_fetch_rejects_plaintext_before_network_in_strict_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOMOS_DEPLOY_PROFILE", "production")

    def fail_network(*_args: object, **_kwargs: object) -> None:
        pytest.fail("plaintext key bootstrap attempted network access")

    monkeypatch.setattr(docker_main.urllib.request, "urlopen", fail_network)

    with pytest.raises(TLSConfigurationError, match="explicitly configured mTLS"):
        docker_main._fetch_key_from_council("http://council:9901")


def test_council_key_fetch_rejects_plaintext_before_network_when_tls_enabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("test CA")
    monkeypatch.setenv("NOMOS_DEPLOY_PROFILE", "dev")
    monkeypatch.setenv("EXECUTOR_COUNCIL_TLS_ENABLED", "true")
    monkeypatch.setenv("EXECUTOR_COUNCIL_TLS_CA_FILE", str(ca_file))

    def fail_network(*_args: object, **_kwargs: object) -> None:
        pytest.fail("plaintext key bootstrap attempted network access")

    monkeypatch.setattr(docker_main.urllib.request, "urlopen", fail_network)

    with pytest.raises(TLSConfigurationError, match="must use https://"):
        docker_main._fetch_key_from_council("http://council:9901")


def test_mcp_key_fetch_rejects_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[str] = []

    def reject_redirect(request, **_kwargs):
        requests.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, 302, "Found", {}, None)

    monkeypatch.setattr(docker_main, "urlopen_no_redirect", reject_redirect)

    with pytest.raises(urllib.error.HTTPError) as exc:
        docker_main._mcp_get_public_key("http://council:9901/mcp")

    assert exc.value.code == 302
    assert requests == ["http://council:9901/mcp"]


def test_legacy_key_fetch_rejects_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[str] = []

    def reject_redirect(request, **_kwargs):
        requests.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, 302, "Found", {}, None)

    monkeypatch.setattr(docker_main, "urlopen_no_redirect", reject_redirect)
    monkeypatch.setattr(docker_main, "_mcp_get_public_key", lambda _url: "")
    monkeypatch.setattr(docker_main, "KEY_FETCH_MAX_RETRIES", 1)

    with pytest.raises(RuntimeError, match="Failed to fetch council public key"):
        docker_main._fetch_key_from_council("http://council:9901")

    assert requests == ["http://council:9901/debug/tools/call/council_get_public_key"]
