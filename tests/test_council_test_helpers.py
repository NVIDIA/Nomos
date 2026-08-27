# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared live-probe helpers under scripts/ -- the monitor /mcp proxy Bearer in particular."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
SPEC = importlib.util.spec_from_file_location(
    "council_test_helpers", SCRIPTS / "council_test_helpers.py"
)
assert SPEC is not None
assert SPEC.loader is not None
helpers = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helpers)


def test_proxy_headers_sends_bearer_when_token_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MONITOR_MCP_PROXY_TOKEN", " proxy-secret ")  # pragma: allowlist secret
    expected = {"Authorization": "Bearer proxy-secret"}  # pragma: allowlist secret
    assert helpers.proxy_headers() == expected


@pytest.mark.parametrize("value", [None, "", "   "])
def test_proxy_headers_empty_without_token(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    """An unauthenticated dev proxy keeps working: no header at all, not an empty Bearer."""
    if value is None:
        monkeypatch.delenv("MONITOR_MCP_PROXY_TOKEN", raising=False)
    else:
        monkeypatch.setenv("MONITOR_MCP_PROXY_TOKEN", value)
    assert helpers.proxy_headers() == {}


def test_every_script_mcp_client_authenticates_to_the_proxy() -> None:
    """Every live probe under scripts/ must send the proxy Bearer.

    The deploy scripts gate the monitor's /mcp proxy by default, so a bare
    streamablehttp_client(...) fails with 401 before any product behaviour runs.
    The pytest live suites and README were fixed one consumer at a time and
    test_smoke.py was missed; this scan makes the next miss a unit-test failure.
    """
    bare: list[str] = []
    for script in sorted(SCRIPTS.glob("*.py")):
        text = script.read_text(encoding="utf-8")
        for match in re.finditer(r"streamablehttp_client\(", text):
            if "headers=proxy_headers()" not in _call_args(text, match.end()):
                line = text.count("\n", 0, match.start()) + 1
                bare.append(f"{script.relative_to(ROOT)}:{line}")
    assert not bare, f"MCP client calls without headers=proxy_headers(): {bare}"


def _call_args(text: str, start: int) -> str:
    """Return the argument text of the call whose opening paren precedes `start`."""
    depth = 1
    for index in range(start, len(text)):
        depth += {"(": 1, ")": -1}.get(text[index], 0)
        if depth == 0:
            return text[start:index]
    raise AssertionError("unbalanced call")
