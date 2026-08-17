# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit cover for the QA ladder probe's non-network logic.

The probe is a verification tool, so its own failure modes matter: a rung that
accepts any refusal, or that dies on an unreachable executor, reports something
untrue about the system under test.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "qa" / "ladder_probe.py"
SPEC = importlib.util.spec_from_file_location("ladder_probe", SCRIPT)
ladder = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
sys.modules[SPEC.name] = ladder
SPEC.loader.exec_module(ladder)


def _env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    base = {
        "LADDER_MCP_URL": "http://monitor:9901/mcp",
        "LADDER_EXEC_URL": "http://executor:9906",
        "NOMOS_AGENT_ID": "agent-developer",
        "NOMOS_AUTH_TOKEN": "t" * 64,  # pragma: allowlist secret
    }
    base.update(overrides)
    for key in (*base, "LADDER_WORKSPACE", "LADDER_PROXY_TOKEN", "LADDER_COUNCIL_ID"):
        monkeypatch.delenv(key, raising=False)
    for key, value in base.items():
        monkeypatch.setenv(key, value)


def test_missing_configuration_names_what_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A KeyError at import tells an operator nothing about which variable to set."""
    _env(monkeypatch)
    monkeypatch.delenv("NOMOS_AUTH_TOKEN")

    with pytest.raises(ladder.ConfigError) as exc:
        ladder.load_config()

    assert "NOMOS_AUTH_TOKEN" in str(exc.value)


@pytest.mark.parametrize(
    "workspace",
    ["/tmp/x; rm -rf /", "relative/path", "/work$(whoami)", "/work`id`", "/work'; touch pwned;'"],
)
def test_hostile_workspace_is_refused(monkeypatch: pytest.MonkeyPatch, workspace: str) -> None:
    """LADDER_WORKSPACE is interpolated into the command the council authorizes."""
    _env(monkeypatch, LADDER_WORKSPACE=workspace)

    with pytest.raises(ladder.ConfigError, match="LADDER_WORKSPACE"):
        ladder.load_config()


def test_ordinary_workspace_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, LADDER_WORKSPACE="/workspace/sub-dir_1")
    assert ladder.load_config()["workspace"] == "/workspace/sub-dir_1"


def test_transport_failure_is_not_a_passing_refusal() -> None:
    """An unreachable executor refuses everything; that is not evidence of a control."""
    ok, detail = ladder.refused_because(
        {"success": False, "error": "connection refused", "transport_error": True}, "nonce"
    )
    assert ok is False
    assert "transport" in detail


def test_refusal_for_the_wrong_reason_fails() -> None:
    """This is the exact failure this branch shipped: rejected on a missing header."""
    ok, detail = ladder.refused_because(
        {"success": False, "error": "Missing required X-Agent-ID header"}, "nonce", "replay"
    )
    assert ok is False
    assert "wrong reason" in detail


def test_refusal_for_the_right_reason_passes() -> None:
    ok, _ = ladder.refused_because(
        {"success": False, "error": "Nonce already claimed (replay detected)"}, "nonce", "replay"
    )
    assert ok is True

    ok, _ = ladder.refused_because(
        {"success": False, "error": "Signature verification failed"}, "signature"
    )
    assert ok is True


def test_success_is_never_a_refusal() -> None:
    ok, detail = ladder.refused_because({"success": True, "exit_code": 0}, "nonce")
    assert ok is False
    assert "NOT refused" in detail


def test_submit_writ_converts_transport_errors_into_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead executor must produce a recordable FAIL, not abort the whole ladder."""
    import urllib.error

    def boom(*_args, **_kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(ladder.urllib.request, "urlopen", boom)

    result = ladder.submit_writ(
        {"exec_url": "http://executor:9906", "agent_id": "agent-developer"}, "blob"
    )

    assert result["success"] is False
    assert result["transport_error"] is True
