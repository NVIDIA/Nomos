# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""The council sizes NAT's per-tool workflow concurrency from the environment."""

from __future__ import annotations

import pytest

pytest.importorskip("nat.plugins.mcp.server.front_end_plugin_worker")

from nomos.nat import stateless_worker as sw


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 8), ("", 8), ("32", 32), (" 16 ", 16), ("0", 0), ("-4", 0), ("lots", 8)],
)
def test_workflow_max_concurrency_reads_the_environment(monkeypatch, raw, expected) -> None:
    if raw is None:
        monkeypatch.delenv(sw.WORKFLOW_MAX_CONCURRENCY_ENV, raising=False)
    else:
        monkeypatch.setenv(sw.WORKFLOW_MAX_CONCURRENCY_ENV, raw)
    assert sw.workflow_max_concurrency() == expected


@pytest.mark.asyncio
async def test_sized_session_manager_passes_the_configured_limit(monkeypatch) -> None:
    seen: dict[str, object] = {}

    async def fake_create(cls, *args, **kwargs):
        seen.update(kwargs)
        return "manager"

    monkeypatch.setattr(sw.SessionManager, "create", classmethod(fake_create))
    monkeypatch.setenv(sw.WORKFLOW_MAX_CONCURRENCY_ENV, "24")

    assert await sw._SizedSessionManager.create(config=None, shared_builder=None) == "manager"
    assert seen["max_concurrency"] == 24

    # An explicit caller value is left alone.
    await sw._SizedSessionManager.create(config=None, shared_builder=None, max_concurrency=3)
    assert seen["max_concurrency"] == 3
