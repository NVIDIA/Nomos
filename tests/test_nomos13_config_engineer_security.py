# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Engineer regressions for bounded, isolated runtime schema validation."""

import os
import signal
import subprocess
import sys
import time

import pytest

PROBE = r"""
import subprocess
import sys
import time
from nomos.level_0.tool_schema import ToolSchemaError, validate_tool_arguments

children = []
original = subprocess.Popen

def record(*args, **kwargs):
    process = original(*args, **kwargs)
    children.append(process)
    return process

subprocess.Popen = record
schema = {"type": "object", "properties": {"value": {"type": "string", "pattern": "^(a+)+$"}},
          "required": ["value"]}
value = "aaaa" if sys.argv[1] == "valid" else "a" * 32 + "!"
started = time.monotonic()
try:
    validate_tool_arguments(schema, {"value": value})
except ToolSchemaError:
    assert sys.argv[1] == "attack"
    assert time.monotonic() - started < 2.6
    assert children and all(child.poll() is not None for child in children)
    print("refused; worker reaped")
else:
    assert sys.argv[1] == "valid"
    print("valid")
"""


def _probe(mode):
    process = subprocess.Popen(
        [sys.executable, "-c", PROBE, mode],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        output, error = process.communicate(timeout=3.5)
    except subprocess.TimeoutExpired:
        # The old implementation must never hang pytest, nor leave its group.
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=1)
        pytest.fail("validation exceeded outer 3.5s safety timeout")
    assert process.returncode == 0, error.decode()
    return output.decode().strip()


def test_cpu_exhaustion_is_refused_and_worker_reaped():
    assert _probe("valid") == "valid"
    assert _probe("attack") == "refused; worker reaped"


def _schema():
    return {
        "type": "object",
        "properties": {"value": {"type": "string", "pattern": "^(a+)+$"}},
        "required": ["value"],
    }


def test_concurrent_attacks_have_at_most_four_children_and_all_are_reaped(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from nomos.level_0 import tool_schema

    original = subprocess.Popen
    lock = threading.Lock()
    children = []
    peaks = []
    barrier = threading.Barrier(12)

    def record(*args, **kwargs):
        with lock:
            process = original(*args, **kwargs)
            children.append(process)
            peaks.append(sum(child.poll() is None for child in children))
            return process

    monkeypatch.setattr(subprocess, "Popen", record)

    def attack(_):
        barrier.wait(timeout=3)
        started = time.monotonic()
        with pytest.raises(tool_schema.ToolSchemaError):
            tool_schema.validate_tool_arguments(_schema(), {"value": "a" * 32 + "!"})
        return time.monotonic() - started

    with ThreadPoolExecutor(max_workers=12) as pool:
        elapsed = list(pool.map(attack, range(12)))
    assert max(elapsed) < 2.8
    assert children and max(peaks) <= 4
    assert all(child.poll() is not None for child in children)
    print(f"12 attacks: peak_workers={max(peaks)}, max_seconds={max(elapsed):.3f}, all_reaped=True")
    # No exhausted/leaked admission slots after the attack.
    tool_schema.validate_tool_arguments(_schema(), {"value": "aaaa"})


def test_full_admission_budget_expires_without_starting_a_child(monkeypatch):
    from nomos.level_0 import tool_schema

    def forbidden(*args, **kwargs):
        pytest.fail("worker started without an admission slot")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    for _ in range(4):
        assert tool_schema._WORKER_SLOTS.acquire(blocking=False)
    try:
        started = time.monotonic()
        with pytest.raises(tool_schema.ToolSchemaError):
            tool_schema.validate_tool_arguments({"type": "object"}, {})
        assert 1.9 < time.monotonic() - started < 2.5
    finally:
        for _ in range(4):
            tool_schema._WORKER_SLOTS.release()


@pytest.mark.parametrize("failure", [OSError("start failed"), RuntimeError("start failed")])
def test_start_failure_denies_and_releases_slot(monkeypatch, failure):
    from nomos.level_0 import tool_schema

    def fail(*args, **kwargs):
        raise failure

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "Popen", fail)
        with pytest.raises(tool_schema.ToolSchemaError, match="unavailable or failed"):
            tool_schema.validate_tool_arguments({"type": "object"}, {})
    tool_schema.validate_tool_arguments({"type": "object"}, {})


@pytest.mark.parametrize(
    "code",
    [
        "import os; os._exit(7)",
        "import os; os.kill(os.getpid(), 9)",
        "import sys; sys.stdout.buffer.write(b'1x')",
        "import sys; sys.stdout.buffer.write(b'')",
        "import sys; sys.stdout.buffer.write(b'0')",
        "import sys; sys.stdout.buffer.write(b'1'); sys.exit(1)",
    ],
)
def test_crash_or_invalid_result_denies_and_reaps(monkeypatch, code):
    from nomos.level_0 import tool_schema

    original = subprocess.Popen
    children = []

    def replacement(*args, **kwargs):
        process = original([sys.executable, "-I", "-c", code], **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", replacement)
    with pytest.raises(tool_schema.ToolSchemaError):
        tool_schema.validate_tool_arguments({"type": "object"}, {})
    assert len(children) == 1 and children[0].poll() is not None
    assert children[0].stdin.closed and children[0].stdout.closed


def test_sigterm_resistant_worker_is_killed_and_reaped(monkeypatch):
    from nomos.level_0 import tool_schema

    original = subprocess.Popen
    children = []
    code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"

    def replacement(*args, **kwargs):
        process = original([sys.executable, "-I", "-c", code], **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", replacement)
    started = time.monotonic()
    with pytest.raises(tool_schema.ToolSchemaError):
        tool_schema.validate_tool_arguments({"type": "object"}, {})
    assert time.monotonic() - started < 2.6
    assert children[0].returncode == -signal.SIGKILL
    assert children[0].stdin.closed and children[0].stdout.closed


def test_startup_time_consumes_deadline_and_worker_is_reaped(monkeypatch):
    from nomos.level_0 import tool_schema

    original = subprocess.Popen
    children = []

    def slow_start(*args, **kwargs):
        time.sleep(1.8)
        process = original([sys.executable, "-I", "-c", "import time; time.sleep(60)"], **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", slow_start)
    started = time.monotonic()
    with pytest.raises(tool_schema.ToolSchemaError):
        tool_schema.validate_tool_arguments({"type": "object"}, {})
    assert time.monotonic() - started < 2.6
    assert children[0].poll() is not None


def test_worker_semantics_and_offline_spies_run_inside_worker_module(monkeypatch):
    import builtins
    import io
    import urllib.request
    from types import SimpleNamespace

    from nomos.level_0 import _tool_schema_worker

    def forbidden(*args, **kwargs):
        pytest.fail("worker attempted schema resource I/O")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    requests = [
        (b'[{"type":"object","properties":{"n":{"type":"integer"}}},{"n":1.0}]', b"1"),
        (b'[{"type":"object","$ref":"https://example.test/denied"},{}]', b"0"),
        (b'[{"type":"object","$ref":"file:///tmp/denied"},{}]', b"0"),
        (b"[", b"0"),
        (b"x" * 64004, b"0"),
    ]
    for payload, expected in requests:
        output = io.BytesIO()
        monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(payload)))
        monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=output))
        _tool_schema_worker.main()
        assert output.getvalue() == expected


def test_static_worker_works_from_stdin_and_does_not_import_caller_main():
    process = subprocess.run(
        [sys.executable, "-"],
        input=(
            "from nomos.level_0.tool_schema import validate_tool_arguments\n"
            "from concurrent.futures import ThreadPoolExecutor\n"
            "with ThreadPoolExecutor(1) as pool:\n"
            "    pool.submit(validate_tool_arguments, {'type': 'object'}, {}).result()\n"
            "print('stdin thread valid')\n"
        ),
        capture_output=True,
        text=True,
        timeout=4,
    )
    assert process.returncode == 0, process.stderr
    assert process.stdout.strip() == "stdin thread valid"


def test_admission_wait_and_validation_share_one_deadline(monkeypatch):
    import threading

    from nomos.level_0 import tool_schema

    original = subprocess.Popen
    children = []

    def record(*args, **kwargs):
        process = original(*args, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", record)
    for _ in range(4):
        assert tool_schema._WORKER_SLOTS.acquire(blocking=False)
    release = threading.Timer(1.7, tool_schema._WORKER_SLOTS.release)
    release.start()
    try:
        started = time.monotonic()
        with pytest.raises(tool_schema.ToolSchemaError):
            tool_schema.validate_tool_arguments(_schema(), {"value": "a" * 28 + "!"})
        assert 1.9 < time.monotonic() - started < 2.6
        assert len(children) == 1 and children[0].poll() is not None
    finally:
        release.join(timeout=1)
        for _ in range(3):
            tool_schema._WORKER_SLOTS.release()


def test_exact_reproduced_28_character_attack_is_bounded():
    from nomos.level_0 import tool_schema

    started = time.monotonic()
    with pytest.raises(tool_schema.ToolSchemaError):
        tool_schema.validate_tool_arguments(_schema(), {"value": "a" * 28 + "!"})
    assert time.monotonic() - started < 2.6


def test_start_finishing_after_deadline_is_denied_and_reaped(monkeypatch):
    from nomos.level_0 import tool_schema

    original = subprocess.Popen
    children = []

    def slow_start(*args, **kwargs):
        time.sleep(2.05)
        process = original([sys.executable, "-I", "-c", "import time; time.sleep(60)"], **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", slow_start)
    with pytest.raises(tool_schema.ToolSchemaError):
        tool_schema.validate_tool_arguments({"type": "object"}, {})
    assert len(children) == 1 and children[0].poll() is not None


def test_local_allof_dag_exhaustion_is_bounded_with_positive_control():
    from nomos.level_0 import tool_schema

    def dag(depth):
        definitions = {"level_0": True}
        for level in range(1, depth + 1):
            reference = {"$ref": f"#/$defs/level_{level - 1}"}
            definitions[f"level_{level}"] = {"allOf": [reference, reference]}
        return {"type": "object", "$defs": definitions, "$ref": f"#/$defs/level_{depth}"}

    tool_schema.validate_tool_arguments(dag(4), {})
    schema = dag(27)
    tool_schema.validate_tool_schema(schema)
    started = time.monotonic()
    with pytest.raises(tool_schema.ToolSchemaError):
        tool_schema.validate_tool_arguments(schema, {})
    assert time.monotonic() - started < 2.6


def test_cleanup_attempts_kill_if_terminate_fails(monkeypatch):
    from nomos.level_0 import tool_schema

    process = subprocess.Popen(
        [sys.executable, "-I", "-c", "import time; time.sleep(60)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )

    def failed_terminate():
        raise OSError("terminate failed")

    monkeypatch.setattr(process, "terminate", failed_terminate)
    try:
        tool_schema._stop_worker(process)
        assert process.poll() is not None
    finally:
        # Keep a RED implementation from leaving this test's intentional sleeper.
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1)
