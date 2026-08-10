# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import shutil
import subprocess
from pathlib import Path

import pytest


def test_copy_button_success_unavailable_rejection_and_double_click() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise the agent guide's browser script")

    guide = Path(__file__).parents[1] / "docs/guides/03-agent-integration.html"
    html = guide.read_text()
    copy_script = "var copyResetTimers" + html.split(
        "  var copyResetTimers", 1
    )[1].split("  document.querySelectorAll('.part')", 1)[0]

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
    {{textContent: 'copy agent.json agent.backup.json\\n'}},
    {{textContent: 'nomos-agent --config agent.json'}},
  ],
}};

const writes = [];
Object.defineProperty(globalThis, 'navigator', {{value: {{
  clipboard: {{writeText: (text) => {{ writes.push(text); return Promise.resolve(); }}}},
}}, configurable: true}});

{copy_script}

(async () => {{
  copyCode.call(button);
  await Promise.resolve();
  advance(600);
  copyCode.call(button);
  await Promise.resolve();

  assert.deepEqual(writes, [
    'copy agent.json agent.backup.json\\nnomos-agent --config agent.json',
    'copy agent.json agent.backup.json\\nnomos-agent --config agent.json',
  ]);
  advance(600);
  assert.equal(button.textContent, 'copied');
  assert.equal(classes.has('ok'), true);
  advance(600);
  assert.equal(button.textContent, 'copy');
  assert.equal(classes.has('ok'), false);

  Object.defineProperty(globalThis, 'navigator', {{value: {{}}, configurable: true}});
  copyCode.call(button);
  assert.equal(button.textContent, 'copy');
  assert.equal(classes.has('ok'), false);

  Object.defineProperty(globalThis, 'navigator', {{value: {{
    clipboard: {{}},
  }}, configurable: true}});
  copyCode.call(button);
  assert.equal(button.textContent, 'copy');
  assert.equal(classes.has('ok'), false);

  const unhandledRejections = [];
  process.on('unhandledRejection', (error) => unhandledRejections.push(error));
  Object.defineProperty(globalThis, 'navigator', {{value: {{
    clipboard: {{writeText: () => Promise.reject(new Error('clipboard denied'))}},
  }}, configurable: true}});
  copyCode.call(button);
  await new Promise(setImmediate);
  assert.equal(button.textContent, 'copy');
  assert.equal(classes.has('ok'), false);
  assert.deepEqual(unhandledRejections, []);
}})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
"""

    subprocess.run([node, "-e", harness], check=True)
