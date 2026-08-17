# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import shutil
import subprocess
from pathlib import Path

import pytest


def test_copy_button_success_unavailable_rejection_and_double_click() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise the deployment guide's browser script")

    guide = Path(__file__).parents[1] / "docs/guides/01-council-cluster-deployment.html"
    html = guide.read_text()
    copy_script = html.split("  // copy buttons\n", 1)[1].split("\n\n  go();", 1)[0]

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
  addEventListener: (_event, listener) => {{ button.click = listener; }},
}};
button.parentElement = {{
  childNodes: [
    button,
    {{textContent: 'copy source.txt target.txt\\n'}},
    {{textContent: 'printf "done"'}},
  ],
}};

const writes = [];
Object.defineProperty(globalThis, 'navigator', {{value: {{
  clipboard: {{writeText: (text) => {{ writes.push(text); return Promise.resolve(); }}}},
}}, configurable: true}});
global.document = {{querySelectorAll: () => [button]}};

{copy_script}

(async () => {{
  button.click.call(button);
  await Promise.resolve();
  advance(600);
  button.click.call(button);
  await Promise.resolve();

  assert.deepEqual(writes, [
    'copy source.txt target.txt\\nprintf "done"',
    'copy source.txt target.txt\\nprintf "done"',
  ]);
  advance(600);
  assert.equal(button.textContent, 'copied');
  assert.equal(classes.has('ok'), true);
  advance(600);
  assert.equal(button.textContent, 'copy');
  assert.equal(classes.has('ok'), false);

  Object.defineProperty(globalThis, 'navigator', {{value: {{}}, configurable: true}});
  button.click.call(button);
  assert.equal(button.textContent, 'copy');
  assert.equal(classes.has('ok'), false);

  Object.defineProperty(globalThis, 'navigator', {{value: {{
    clipboard: {{}},
  }}, configurable: true}});
  button.click.call(button);
  assert.equal(button.textContent, 'copy');
  assert.equal(classes.has('ok'), false);

  const unhandledRejections = [];
  process.on('unhandledRejection', (error) => unhandledRejections.push(error));
  Object.defineProperty(globalThis, 'navigator', {{value: {{
    clipboard: {{writeText: () => Promise.reject(new Error('clipboard denied'))}},
  }}, configurable: true}});
  button.click.call(button);
  await new Promise(setImmediate);
  assert.equal(button.textContent, 'copy');
  assert.equal(classes.has('ok'), false);
  assert.deepEqual(unhandledRejections, []);
}})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
"""

    subprocess.run([node, "-e", harness], check=True)
