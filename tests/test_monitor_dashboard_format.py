# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Runs the dashboard's wording tests (Node) and checks the page loads its scripts."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
STATIC = ROOT / "monitor" / "static"


def test_dashboard_wording() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to run the dashboard's wording tests")
    result = subprocess.run(
        [node, "--test", str(ROOT / "tests" / "monitor_dashboard_format.test.mjs")],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_page_loads_the_wording_before_the_scripts_that_use_it() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    order = [html.index(f"/static/{name}") for name in ("format.js", "app.js", "pipeline.js")]
    assert order == sorted(order)


def test_every_element_the_page_script_looks_up_exists() -> None:
    """A renamed id fails silently in the browser: the element lookup returns null."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    wanted = set(re.findall(r'\$\("([^"]+)"\)', script))
    assert wanted - ids == set()
