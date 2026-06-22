# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


from __future__ import annotations

import asyncio

import pytest

from executors.docker import main as docker_main


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
