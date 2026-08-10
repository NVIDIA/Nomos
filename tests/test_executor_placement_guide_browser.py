# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import shutil
import subprocess
from pathlib import Path

import pytest


def test_copy_button_double_click_keeps_code_and_restarts_reset_timer() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise the executor guide's browser script")

    guide = Path(__file__).parents[1] / "docs/guides/02-executor-placement.html"
    html = guide.read_text()
    copy_script = "var copyResetTimers" + html.split(
        "  var copyResetTimers", 1
    )[1].split("  document.querySelectorAll('.pick')", 1)[0]

    harness = f"""
const assert = require('node:assert/strict');

let now = 0;
let nextTimer = 1;
const timers = [];
global.setTimeout = (callback, delay, ...args) => {{
  const timer = {{id: nextTimer++, due: now + delay, callback, args, cancelled: false}};
  timers.push(timer);
  return timer.id;
}};
global.clearTimeout = (id) => {{
  const timer = timers.find((candidate) => candidate.id === id);
  if (timer) timer.cancelled = true;
}};
function advance(milliseconds) {{
  now += milliseconds;
  for (const timer of timers.filter((candidate) => !candidate.cancelled && candidate.due <= now)) {{
    timer.cancelled = true;
    timer.callback(...timer.args);
  }}
}}

const classes = new Set();
const button = {{
  textContent: 'copy',
  classList: {{
    add: (name) => classes.add(name),
    remove: (name) => classes.delete(name),
  }},
}};
button.parentElement = {{
  childNodes: [
    button,
    {{textContent: 'copy config.yaml /etc/nomos/config.yaml\\n'}},
    {{textContent: 'nomos-executor --config /etc/nomos/config.yaml'}},
  ],
}};

const writes = [];
Object.defineProperty(globalThis, 'navigator', {{value: {{
  clipboard: {{writeText: (text) => {{ writes.push(text); return Promise.resolve(); }}}},
}}}});

{copy_script}

(async () => {{
  copyCode.call(button);
  await Promise.resolve();
  advance(600);
  copyCode.call(button);
  await Promise.resolve();

  assert.deepEqual(writes, [
    'copy config.yaml /etc/nomos/config.yaml\\nnomos-executor --config /etc/nomos/config.yaml',
    'copy config.yaml /etc/nomos/config.yaml\\nnomos-executor --config /etc/nomos/config.yaml',
  ]);
  advance(600);
  assert.equal(button.textContent, 'copied');
  assert.equal(classes.has('ok'), true);
  advance(600);
  assert.equal(button.textContent, 'copy');
  assert.equal(classes.has('ok'), false);
}})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
"""

    subprocess.run([node, "-e", harness], check=True)
