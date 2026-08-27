# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Stubbed regression coverage for live-E2E council status handling."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "test_live_e2e.py"


@pytest.fixture
def live_e2e(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load the executable script without using pytest's command-line arguments."""
    monkeypatch.setattr(sys, "argv", [str(SCRIPT)])
    # The script imports its sibling council_test_helpers the way `python scripts/x.py` does.
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("live_e2e_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "pause_between_scenarios", lambda: None)
    return module


def auth_response(status: str, **extra: Any) -> dict[str, Any]:
    return {
        "status": status,
        "writ_id": extra.pop("writ_id", "N/A"),
        "council_members": extra.pop("council_members", []),
        **extra,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bare_status", "absolute_status", "expected", "message"),
    [
        pytest.param("denied", "approved", True, "PASSED", id="expected-denied-approved"),
        pytest.param(
            "approved", "approved", False, "full-path rule not enforced", id="bare-approved"
        ),
        pytest.param("error", "approved", False, "prerequisite failure", id="bare-error"),
        pytest.param("unknown", "approved", False, "UNKNOWN", id="bare-unknown"),
        pytest.param("denied", "denied", False, "was DENIED", id="absolute-denied"),
        pytest.param("denied", "error", False, "prerequisite failure", id="absolute-error"),
        pytest.param("denied", "unknown", False, "UNKNOWN", id="absolute-unknown"),
    ],
)
async def test_full_path_classifies_every_council_status(
    live_e2e: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    bare_status: str,
    absolute_status: str,
    expected: bool,
    message: str,
) -> None:
    responses = iter(
        [
            auth_response(bare_status, error="backend unavailable"),
            auth_response(absolute_status, error="backend unavailable"),
        ]
    )

    async def fake_authorize(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return next(responses)

    monkeypatch.setattr(live_e2e, "authorize", fake_authorize)

    assert await live_e2e.test_full_path_compliance(object()) is expected
    assert message in capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected", "decision"),
    [
        pytest.param("approved", False, "APPROVED", id="approved"),
        pytest.param("denied", True, "DENIED", id="denied"),
        pytest.param("error", False, "ERROR", id="error"),
        pytest.param("unknown", False, "UNKNOWN", id="unknown"),
    ],
)
async def test_destructive_command_classifies_every_council_status(
    live_e2e: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
    expected: bool,
    decision: str,
) -> None:
    async def fake_authorize(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return auth_response(status, error="backend unavailable")

    monkeypatch.setattr(live_e2e, "authorize", fake_authorize)

    assert await live_e2e.test_destructive_command_rejection(object()) is expected
    output = capsys.readouterr().out
    assert decision in output
    assert ("PASSED" in output) is (status == "denied")


@pytest.mark.asyncio
async def test_main_exits_nonzero_when_council_prerequisite_errors(
    live_e2e: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeTransport:
        async def __aenter__(self) -> tuple[object, object, object]:
            return object(), object(), object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class FakeSession:
        def __init__(self, *_args: object) -> None:
            pass

        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def initialize(self) -> None:
            return None

    responses = iter(
        [auth_response("error", error="backend unavailable"), auth_response("approved")]
    )

    async def fake_authorize(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return next(responses)

    async def fake_authorized_execution(_session: object) -> tuple[bool, None]:
        return True, None

    async def fake_destructive(_session: object) -> bool:
        return True

    monkeypatch.setattr(
        live_e2e, "streamablehttp_client", lambda *_args, **_kwargs: FakeTransport()
    )
    monkeypatch.setattr(live_e2e, "ClientSession", FakeSession)
    monkeypatch.setattr(live_e2e, "check_executor_health", lambda: {"key_fingerprint": "stub"})
    monkeypatch.setattr(
        live_e2e,
        "docker_exec",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "", ""),
    )
    monkeypatch.setattr(live_e2e, "authorize", fake_authorize)
    monkeypatch.setattr(live_e2e, "test_authorized_execution", fake_authorized_execution)
    monkeypatch.setattr(live_e2e, "test_destructive_command_rejection", fake_destructive)

    with pytest.raises(SystemExit) as exc_info:
        await live_e2e.main()

    assert exc_info.value.code == 1
    output = capsys.readouterr().out
    assert "ERROR" in output
    assert "prerequisite failure" in output
    assert "Some scenarios failed" in output
