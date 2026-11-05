# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from demo.server import executor as executor_module


@pytest.mark.asyncio
async def test_health_returns_invalid_status_for_non_json_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        def json(self):
            raise ValueError("not JSON")

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url):
            return FakeResponse()

    monkeypatch.setattr(executor_module.httpx, "AsyncClient", FakeClient)

    client = executor_module.ExecutorClient("http://executor:9902")

    assert await client.health() == {"status": "invalid response"}
