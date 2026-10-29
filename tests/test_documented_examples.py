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

import builtins
import html
import re
import symtable
from pathlib import Path

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
