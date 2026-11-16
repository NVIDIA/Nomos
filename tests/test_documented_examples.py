# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Documented examples must at least be runnable Python.

A reviewer found that the agent-integration guide's client raised `NameError`
on its first call: `**kw` was used in `main()` while `kw` existed only as a
parameter of a different function. Nothing caught it, because nothing ever
looked at the examples -- they are prose to the test suite.

The scope analysis here uses `symtable` deliberately. An earlier attempt
gathered every function's parameters into one flat set, which meant the `kw`
of `propose()` masked its absence in `main()` -- a scope-blind check cannot
find a scoping bug, and it reported the broken example as clean.

This does not execute anything. It asserts the examples parse and that no name
is read which is never bound in an enclosing scope.
"""

from __future__ import annotations

import ast
import builtins
import html
import json
import re
import symtable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]

# HTML guides use <pre class="code">; the leading "copy" is the button label.
_HTML_BLOCK = re.compile(r'<pre class="code"[^>]*>(.*?)</pre>', re.S)
_TAG = re.compile(r"<[^>]+>")
_MD_BLOCK = re.compile(r"```python\n(.*?)```", re.S)


# The guides carry no language marker on their code blocks -- they mix YAML,
# shell and Python -- so Python is selected by content. An earlier version of
# this test parsed every block and reported nine phantom failures for YAML that
# was never Python to begin with.
_LOOKS_PYTHON = re.compile(r"^\s*(def |class |import |from \S+ import )", re.M)

# Blocks that are deliberately NOT checked are counted rather than dropped, so a
# selector that quietly stops matching cannot masquerade as everything passing.
SKIPPED: list[str] = []


def _html_examples(path: Path) -> list[tuple[str, str]]:
    out = []
    for i, block in enumerate(_HTML_BLOCK.findall(path.read_text(encoding="utf-8"))):
        code = re.sub(r"^\s*copy", "", html.unescape(_TAG.sub("", block)), count=1)
        name = f"{path.name}[{i}]"
        if _LOOKS_PYTHON.search(code):
            out.append((name, code))
        else:
            SKIPPED.append(f"{name}: not Python (no def/class/import)")
    return out


def _md_examples(path: Path) -> list[tuple[str, str]]:
    return [
        (f"{path.name}[{i}]", code)
        for i, code in enumerate(_MD_BLOCK.findall(path.read_text(encoding="utf-8")))
    ]


def _all_examples() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for guide in sorted((ROOT / "docs" / "guides").glob("*.html")):
        found += _html_examples(guide)
    # The README is a landing page; the runnable examples live in docs/.
    for page in [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]:
        found += _md_examples(page)
    return found


def _undefined_names(code: str) -> list[tuple[str, str]]:
    """Names read in a scope but bound in none that encloses it."""
    top = symtable.symtable(code, "<example>", "exec")
    module_bound = {
        s.get_name()
        for s in top.get_symbols()
        if s.is_assigned() or s.is_imported() or s.is_namespace()
    }
    bad: list[tuple[str, str]] = []

    def walk(table: symtable.SymbolTable, enclosing: set[str]) -> None:
        local = {
            s.get_name()
            for s in table.get_symbols()
            if s.is_assigned() or s.is_parameter() or s.is_namespace() or s.is_imported()
        }
        for sym in table.get_symbols():
            name = sym.get_name()
            if not sym.is_referenced() or sym.is_assigned() or sym.is_parameter():
                continue
            if name in local or name in enclosing or name in module_bound:
                continue
            if hasattr(builtins, name):
                continue
            bad.append((table.get_name(), name))
        for child in table.get_children():
            walk(child, enclosing | local)

    for child in top.get_children():
        walk(child, set())
    return bad


EXAMPLES = _all_examples()


def test_examples_were_actually_found():
    """A selector that matches nothing would make every assertion below vacuous."""
    assert len(EXAMPLES) >= 4, (
        f"only found {len(EXAMPLES)} Python examples; "
        f"{len(SKIPPED)} blocks were classified as non-Python: {SKIPPED}"
    )
    assert any("def main(" in c for _, c in EXAMPLES), (
        "no self-contained client example found, so the scope check below covers nothing"
    )


@pytest.mark.parametrize("name,code", EXAMPLES, ids=[n for n, _ in EXAMPLES])
def test_documented_example_parses(name: str, code: str):
    try:
        symtable.symtable(code, "<example>", "exec")
    except SyntaxError as e:
        pytest.fail(f"{name} is not valid Python: line {e.lineno}: {e.msg}")


@pytest.mark.parametrize(
    "name,code",
    [(n, c) for n, c in EXAMPLES if "def main(" in c],
    ids=[n for n, c in EXAMPLES if "def main(" in c],
)
def test_self_contained_example_has_no_undefined_names(name: str, code: str):
    """Only blocks with their own `main()` -- fragments legitimately reference
    names defined elsewhere on the page."""
    bad = _undefined_names(code)
    assert not bad, "\n".join(f"{name}: {scope}() reads undefined {n!r}" for scope, n in bad)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "page,function",
    [
        ("README.md", "run_through_nomos"),
        ("docs/integrate-your-agent.md", "nomos_action_adapter"),
    ],
)
@pytest.mark.parametrize(
    "success,stage", [(True, "passed"), (False, "passed"), (False, "signature")]
)
async def test_adapter_preserves_execution_result(page, function, success, stage, monkeypatch):
    """Failed commands may change state; their signed result must remain usable evidence."""
    source = next(code for _, code in _md_examples(ROOT / page) if f"def {function}(" in code)
    definition = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == function
    )
    receipt = {
        "success": success,
        "verification_stage": stage,
        "signature": "test-signature",
        "exit_code": 1 if not success else 0,
    }
    response = MagicMock()
    response.json.return_value = receipt
    client = SimpleNamespace(post=AsyncMock(return_value=response))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=False)
    for key in ("NOMOS_AGENT_ID", "NOMOS_AUTH_TOKEN", "NOMOS_COUNCIL_ID", "NOMOS_EXECUTOR_URL"):
        monkeypatch.setenv(key, "http://executor" if key.endswith("URL") else "test-value")
    import os

    namespace = {
        "json": json,
        "os": os,
        "httpx": SimpleNamespace(AsyncClient=lambda **kwargs: context),
        "AGENT_ID": "test-agent",
        "AUTH_TOKEN": "test-token",
        "EXECUTOR_URL": "http://executor",
    }
    # Execute only the checked-in adapter function with mocked transports.
    exec(compile(ast.Module(body=[definition], type_ignores=[]), page, "exec"), namespace)  # noqa: S102
    session = SimpleNamespace(
        call_tool=AsyncMock(
            return_value=SimpleNamespace(
                isError=False,
                content=[SimpleNamespace(text=json.dumps({"status": "approved", "writ": {}}))],
            )
        )
    )
    result = await namespace[function](session, {})
    assert result is receipt, (
        "return the original result, not a wrapper that cannot be certified evidence"
    )
    response.raise_for_status.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "page,function",
    [
        ("README.md", "run_through_nomos"),
        ("docs/integrate-your-agent.md", "nomos_action_adapter"),
    ],
)
@pytest.mark.parametrize("outcome", ["denied", "error", "tool_error"])
async def test_adapter_never_executes_without_approval(page, function, outcome, monkeypatch):
    """A denial, a request error or an MCP tool error must not reach the executor."""
    source = next(code for _, code in _md_examples(ROOT / page) if f"def {function}(" in code)
    definition = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == function
    )
    client = SimpleNamespace(post=AsyncMock())
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=False)
    for key in ("NOMOS_AGENT_ID", "NOMOS_AUTH_TOKEN", "NOMOS_COUNCIL_ID", "NOMOS_EXECUTOR_URL"):
        monkeypatch.setenv(key, "http://executor" if key.endswith("URL") else "test-value")
    import os

    namespace = {
        "json": json,
        "os": os,
        "httpx": SimpleNamespace(AsyncClient=lambda **kwargs: context),
        "AGENT_ID": "test-agent",
        "AUTH_TOKEN": "test-token",
        "EXECUTOR_URL": "http://executor",
    }
    exec(compile(ast.Module(body=[definition], type_ignores=[]), page, "exec"), namespace)  # noqa: S102
    if outcome == "tool_error":
        reply = SimpleNamespace(isError=True, content=[SimpleNamespace(text="tool failed")])
    else:
        decision = {"status": outcome, "guidance": ["revise"], "error": "why"}
        reply = SimpleNamespace(isError=False, content=[SimpleNamespace(text=json.dumps(decision))])
    session = SimpleNamespace(call_tool=AsyncMock(return_value=reply))
    if outcome == "tool_error":
        with pytest.raises(RuntimeError):
            await namespace[function](session, {})
    else:
        result = await namespace[function](session, {})
        assert outcome in json.dumps(result), "the decision must go back to the agent"
    client.post.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply,exec_result,expected",
    [
        ({"isError": True}, None, "Tool error"),
        ({"status": "denied", "guidance": ["revise"]}, None, "Denied: ['revise']"),
        (
            {"status": "approved", "writ": "w"},
            {"success": False, "verification_stage": "signature", "error": "bad sig"},
            "Rejected at signature: bad sig",
        ),
        (
            {"status": "approved", "writ": "w"},
            {"success": False, "verification_stage": "passed", "exit_code": 2, "stderr": "boom"},
            "Command failed with exit code 2: boom",
        ),
        (
            {"status": "approved", "writ": "w"},
            {"success": True, "verification_stage": "passed", "exit_code": 0, "stdout": "ok"},
            "Exit code: 0",
        ),
    ],
)
async def test_getting_started_walkthrough_reports_each_outcome(
    reply, exec_result, expected, capsys
):
    """The walkthrough must not parse a tool error or execute without approval, and must
    tell a rejected Writ apart from a command that ran and failed."""
    page = "docs/getting-started.md"
    source = next(
        code for _, code in _md_examples(ROOT / page) if "def authorize_and_execute(" in code
    )
    definition = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "authorize_and_execute"
    )
    if reply.get("isError"):
        tool_reply = SimpleNamespace(isError=True, content=[SimpleNamespace(text="tool failed")])
    else:
        tool_reply = SimpleNamespace(
            isError=False, content=[SimpleNamespace(text=json.dumps(reply))]
        )
    session = SimpleNamespace(initialize=AsyncMock(), call_tool=AsyncMock(return_value=tool_reply))

    def async_context(value):
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=value)
        context.__aexit__ = AsyncMock(return_value=False)
        return context

    response = MagicMock()
    response.read.return_value = json.dumps(exec_result).encode()
    opened = MagicMock()
    opened.__enter__.return_value = response
    urlopen = MagicMock(return_value=opened)
    namespace = {
        "json": json,
        "urllib": SimpleNamespace(request=SimpleNamespace(Request=MagicMock(), urlopen=urlopen)),
        "streamablehttp_client": lambda *args, **kwargs: async_context((None, None, None)),
        "ClientSession": lambda *args: async_context(session),
        "AUTH_TOKEN": "test-token",
        "PROXY_TOKEN": "test-proxy",
        "AGENT_ID": "test-agent",
    }
    exec(compile(ast.Module(body=[definition], type_ignores=[]), page, "exec"), namespace)  # noqa: S102
    await namespace["authorize_and_execute"]()
    assert expected in capsys.readouterr().out
    if exec_result is None:
        urlopen.assert_not_called()
