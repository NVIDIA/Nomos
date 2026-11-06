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


@pytest.fixture
def worker_control(monkeypatch):
    import io
    from types import SimpleNamespace

    from nomos.level_0 import tool_schema

    state = SimpleNamespace(
        now=100.0,
        admission=0.0,
        admitted=True,
        startup=0.0,
        communicate=0.0,
        acquired=[],
        releases=0,
        children=[],
    )

    class Slots:
        def acquire(self, *, timeout):
            state.acquired.append(timeout)
            state.now += state.admission
            return state.admitted

        def release(self):
            state.releases += 1

    class Process:
        def __init__(self, *args, **kwargs):
            state.now += state.startup
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO()
            self.returncode = None
            self.timeouts = []
            self.waits = []
            state.children.append(self)

        def communicate(self, payload, *, timeout):
            assert payload == b'[{"type":"object"},{}]'
            self.timeouts.append(timeout)
            state.now += state.communicate
            if state.communicate >= timeout:
                raise subprocess.TimeoutExpired("static-worker", timeout)
            self.returncode = 0
            return b"1", None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -signal.SIGTERM

        def kill(self):
            self.returncode = -signal.SIGKILL

        def wait(self, *, timeout):
            self.waits.append(timeout)
            return self.returncode

    monkeypatch.setattr(tool_schema, "time", SimpleNamespace(monotonic=lambda: state.now))
    monkeypatch.setattr(tool_schema, "_WORKER_SLOTS", Slots())
    monkeypatch.setattr(subprocess, "Popen", Process)
    return state


def test_full_admission_budget_expires_without_starting_a_child(worker_control):
    from nomos.level_0 import tool_schema

    worker_control.admission = 2.0
    worker_control.admitted = False
    with pytest.raises(tool_schema.ToolSchemaError, match="deadline exceeded"):
        tool_schema.validate_tool_arguments({"type": "object"}, {})
    assert worker_control.acquired == [2.0]
    assert not worker_control.children
    assert worker_control.releases == 0


@pytest.mark.parametrize("failure", [OSError("start failed"), RuntimeError("start failed")])
def test_start_failure_denies_and_releases_slot(monkeypatch, failure, worker_control):
    from nomos.level_0 import tool_schema

    def fail(*args, **kwargs):
        raise failure

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "Popen", fail)
        with pytest.raises(tool_schema.ToolSchemaError, match="unavailable or failed"):
            tool_schema.validate_tool_arguments({"type": "object"}, {})
    assert worker_control.releases == 1
    tool_schema.validate_tool_arguments({"type": "object"}, {})
    assert worker_control.releases == 2


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


def test_startup_time_consumes_deadline_and_worker_is_reaped(worker_control):
    from nomos.level_0 import tool_schema

    worker_control.startup = 1.75
    worker_control.communicate = 0.5
    with pytest.raises(tool_schema.ToolSchemaError):
        tool_schema.validate_tool_arguments({"type": "object"}, {})
    (child,) = worker_control.children
    assert child.timeouts == [0.25]
    assert child.waits == [0.1]
    assert child.poll() is not None
    assert child.stdin.closed and child.stdout.closed
    assert worker_control.releases == 1


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


def test_admission_wait_and_validation_share_one_deadline(worker_control):
    from nomos.level_0 import tool_schema

    worker_control.admission = 1.5
    worker_control.startup = 0.25
    worker_control.communicate = 0.5
    with pytest.raises(tool_schema.ToolSchemaError):
        tool_schema.validate_tool_arguments({"type": "object"}, {})
    (child,) = worker_control.children
    assert worker_control.acquired == [2.0]
    assert child.timeouts == [0.25]
    assert child.waits == [0.1]
    assert child.stdin.closed and child.stdout.closed
    assert worker_control.releases == 1
    worker_control.admission = worker_control.startup = worker_control.communicate = 0.0
    tool_schema.validate_tool_arguments({"type": "object"}, {})
    assert worker_control.releases == 2


def test_exact_reproduced_28_character_attack_is_bounded():
    from nomos.level_0 import tool_schema

    started = time.monotonic()
    with pytest.raises(tool_schema.ToolSchemaError):
        tool_schema.validate_tool_arguments(_schema(), {"value": "a" * 28 + "!"})
    assert time.monotonic() - started < 2.6


def test_start_finishing_after_deadline_is_denied_and_reaped(worker_control):
    from nomos.level_0 import tool_schema

    worker_control.startup = 2.25
    with pytest.raises(tool_schema.ToolSchemaError, match="deadline exceeded"):
        tool_schema.validate_tool_arguments({"type": "object"}, {})
    (child,) = worker_control.children
    assert not child.timeouts
    assert child.waits == [0.1]
    assert child.poll() is not None
    assert child.stdin.closed and child.stdout.closed
    assert worker_control.releases == 1


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


@pytest.mark.asyncio
async def test_async_resolver_retains_running_permit_and_context(monkeypatch):
    import asyncio
    import contextvars
    from concurrent.futures import Future

    from nomos.level_0 import tool_catalog

    marker = contextvars.ContextVar("resolver_marker")
    marker.set("request-context")
    jobs = []

    class Executor:
        def submit(self, fn, *args, **kwargs):
            future = Future()
            assert future.set_running_or_notify_cancel()
            jobs.append((future, fn, args, kwargs))
            return future

    def resolve(**kwargs):
        assert marker.get() == "request-context"
        assert kwargs["deadline"] is not None
        return None

    monkeypatch.setattr(tool_catalog, "_RESOLVER_EXECUTOR", Executor())
    monkeypatch.setattr(tool_catalog, "resolve_tool_review_context", resolve)
    resolver = tool_catalog.AsyncToolCatalogResolver()
    tasks = [asyncio.create_task(resolver.resolve(council_id="c", tools=None)) for _ in range(4)]
    for _ in range(20):
        await asyncio.sleep(0)
        if len(jobs) == 4:
            break
    assert len(jobs) == 4
    tasks[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await tasks[0]
    followup = asyncio.create_task(resolver.resolve(council_id="c", tools=None))
    for _ in range(5):
        await asyncio.sleep(0)
    assert len(jobs) == 4
    future, fn, args, kwargs = jobs[0]
    future.set_result(fn(*args, **kwargs))
    for _ in range(20):
        await asyncio.sleep(0)
        if len(jobs) == 5:
            break
    assert len(jobs) == 5
    for future, fn, args, kwargs in jobs[1:]:
        future.set_result(fn(*args, **kwargs))
    assert await asyncio.gather(*tasks[1:], followup) == [None] * 4


@pytest.mark.asyncio
async def test_async_resolver_cancels_queued_job_without_start(monkeypatch):
    import asyncio
    from concurrent.futures import Future

    from nomos.level_0 import tool_catalog

    jobs = []

    class Executor:
        def submit(self, fn, *args, **kwargs):
            future = Future()
            jobs.append(future)
            return future

    monkeypatch.setattr(tool_catalog, "_RESOLVER_EXECUTOR", Executor())
    resolver = tool_catalog.AsyncToolCatalogResolver()
    task = asyncio.create_task(resolver.resolve(council_id="c", tools=None))
    for _ in range(20):
        await asyncio.sleep(0)
        if jobs:
            break
    assert len(jobs) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert jobs[0].cancelled()


@pytest.mark.parametrize("remaining", [0.5, 5.0])
def test_caller_deadline_can_only_shorten_worker_budget(worker_control, remaining):
    from nomos.level_0 import tool_schema

    tool_schema.validate_tool_arguments(
        {"type": "object"}, {}, deadline=worker_control.now + remaining
    )
    assert worker_control.acquired == [min(remaining, 2.0)]
    assert worker_control.children[0].timeouts == [min(remaining, 2.0)]
    assert worker_control.releases == 1


def test_expired_caller_deadline_does_not_acquire_or_spawn(worker_control):
    from nomos.level_0 import tool_schema

    with pytest.raises(tool_schema.ToolSchemaError, match="deadline exceeded"):
        tool_schema.validate_tool_arguments({"type": "object"}, {}, deadline=99.0)
    assert not worker_control.acquired
    assert not worker_control.children
    assert worker_control.releases == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("queue_delay", [0.25, 2.0])
async def test_async_original_deadline_covers_gate_queue_and_worker(
    monkeypatch, worker_control, queue_delay
):
    from concurrent.futures import Future
    from types import SimpleNamespace

    from nomos.level_0 import tool_catalog, tool_schema

    state = worker_control
    observed = []

    class Gate:
        releases = 0

        async def acquire(self):
            state.now += 0.5
            return True

        def release(self):
            self.releases += 1

    class Executor:
        def submit(self, fn, *args, **kwargs):
            state.now += queue_delay
            future = Future()
            try:
                future.set_result(fn(*args, **kwargs))
            except Exception as exc:
                future.set_exception(exc)
            return future

    # Retain the real synchronous resolver, using one benign declared tool.
    from nomos.level_0.types.tool_call import ToolCallRequest
    from nomos.level_0.types.tool_definition import TrustedToolDefinition

    tools = (
        TrustedToolDefinition(
            name="sample",
            description="sample",
            parameters={"type": "object"},
            usage_constraints=("Use the declared arguments",),
        ),
    )
    call = ToolCallRequest(tool_name="sample", arguments={}, subject="test-user")
    real_validate = tool_schema.validate_tool_arguments

    def validate(schema, arguments, *, deadline):
        observed.append(deadline)
        real_validate(schema, arguments, deadline=deadline)

    monkeypatch.setattr(tool_catalog, "time", SimpleNamespace(monotonic=lambda: state.now))
    monkeypatch.setattr(tool_catalog, "_RESOLVER_EXECUTOR", Executor())
    monkeypatch.setattr(tool_catalog, "validate_tool_arguments", validate)
    resolver = tool_catalog.AsyncToolCatalogResolver()
    gate = Gate()
    resolver._admission = gate
    if queue_delay >= 2.0:
        with pytest.raises(tool_catalog.ToolCatalogError):
            await resolver.resolve(council_id="c", tools=tools, tool_call=call)
        assert not state.children
        assert not observed
    else:
        result = await resolver.resolve(council_id="c", tools=tools, tool_call=call)
        assert result.matched_tool_name == "sample"
        assert observed == [102.0]
        assert state.children[0].timeouts == [1.25]
    assert gate.releases == 1


@pytest.mark.asyncio
async def test_async_cancellation_before_admission_submits_nothing(monkeypatch):
    import asyncio

    from nomos.level_0 import tool_catalog

    class Executor:
        def submit(self, *args, **kwargs):
            pytest.fail("cancelled admission submitted a job")

    monkeypatch.setattr(tool_catalog, "_RESOLVER_EXECUTOR", Executor())
    resolver = tool_catalog.AsyncToolCatalogResolver()
    for _ in range(4):
        await resolver._admission.acquire()
    task = asyncio.create_task(resolver.resolve(council_id="c", tools=None))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(4):
        resolver._admission.release()
    assert resolver._admission._value == 4


@pytest.mark.asyncio
async def test_async_expired_admission_and_submit_failure_recover(monkeypatch, worker_control):
    from types import SimpleNamespace

    from nomos.level_0 import tool_catalog

    state = worker_control

    class Gate:
        releases = 0

        async def acquire(self):
            state.now += 2.0
            return True

        def release(self):
            self.releases += 1

    class Executor:
        def submit(self, *args, **kwargs):
            pytest.fail("expired admission submitted a job")

    monkeypatch.setattr(tool_catalog, "time", SimpleNamespace(monotonic=lambda: state.now))
    monkeypatch.setattr(tool_catalog, "_RESOLVER_EXECUTOR", Executor())
    resolver = tool_catalog.AsyncToolCatalogResolver()
    gate = Gate()
    resolver._admission = gate
    with pytest.raises(tool_catalog.ToolCatalogError):
        await resolver.resolve(council_id="c", tools=None)
    assert gate.releases == 1

    class FailedExecutor:
        def submit(self, *args, **kwargs):
            raise RuntimeError("dummy startup details")

    monkeypatch.setattr(tool_catalog, "_RESOLVER_EXECUTOR", FailedExecutor())
    resolver = tool_catalog.AsyncToolCatalogResolver()
    with pytest.raises(
        tool_catalog.ToolCatalogError, match=r"^tool catalog resolution unavailable or failed$"
    ):
        await resolver.resolve(council_id="c", tools=None)
    assert resolver._admission._value == 4


@pytest.mark.asyncio
async def test_async_timeout_keeps_running_job_reserved(monkeypatch):
    import asyncio
    from concurrent.futures import Future

    from nomos.level_0 import tool_catalog

    jobs = []

    class Executor:
        def submit(self, *args, **kwargs):
            future = Future()
            assert future.set_running_or_notify_cancel()
            jobs.append(future)
            return future

    monkeypatch.setattr(tool_catalog, "_RESOLVER_EXECUTOR", Executor())
    resolver = tool_catalog.AsyncToolCatalogResolver()
    # Exercise the result timeout separately from the admission timeout.
    original_wait = asyncio.wait_for
    calls = 0

    async def wait(awaitable, timeout):
        nonlocal calls
        calls += 1
        return await original_wait(awaitable, timeout=1 if calls == 1 else 0)

    # A fixed clock permits submission with a positive original budget; the
    # result wait is the controlled timeout above.
    monkeypatch.setattr(tool_catalog, "_ARGUMENT_VALIDATION_SECONDS", 2.0)
    monkeypatch.setattr(asyncio, "wait_for", wait)
    with pytest.raises(tool_catalog.ToolCatalogError, match="deadline exceeded"):
        await resolver.resolve(council_id="c", tools=None)
    assert len(jobs) == 1
    assert resolver._admission._value == 3
    jobs[0].set_result(None)
    await asyncio.sleep(0)
    assert resolver._admission._value == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("detach", ["timeout", "cancel"])
@pytest.mark.parametrize("outcome", ["catalog_error", "unexpected_error", "success"])
async def test_async_detached_future_completion_is_observed(monkeypatch, detach, outcome):
    import asyncio
    import gc
    import weakref
    from concurrent.futures import Future

    from nomos.level_0 import tool_catalog

    jobs = []
    wrappers = []
    diagnostics = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    original_wrap = asyncio.wrap_future
    original_wait = asyncio.wait_for
    waits = 0

    class Executor:
        def submit(self, *args, **kwargs):
            future = Future()
            assert future.set_running_or_notify_cancel()
            jobs.append(future)
            return future

    def wrap(future, *, loop):
        wrapped = original_wrap(future, loop=loop)
        wrappers.append(weakref.ref(wrapped))
        return wrapped

    async def wait(awaitable, timeout):
        nonlocal waits
        waits += 1
        if detach == "timeout" and waits == 2:
            timeout = 0
        return await original_wait(awaitable, timeout=timeout)

    monkeypatch.setattr(tool_catalog, "_RESOLVER_EXECUTOR", Executor())
    monkeypatch.setattr(asyncio, "wrap_future", wrap)
    monkeypatch.setattr(asyncio, "wait_for", wait)
    loop.set_exception_handler(lambda _loop, context: diagnostics.append(context))
    try:
        resolver = tool_catalog.AsyncToolCatalogResolver()
        task = asyncio.create_task(resolver.resolve(council_id="c", tools=None))
        for _ in range(20):
            await asyncio.sleep(0)
            if wrappers:
                break
        assert len(jobs) == len(wrappers) == 1
        if detach == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(tool_catalog.ToolCatalogError, match="deadline exceeded"):
                await task
        del task
        assert not jobs[0].cancelled()
        assert not jobs[0].done()
        assert resolver._admission._value == 3
        if outcome == "catalog_error":
            jobs[0].set_exception(tool_catalog.ToolCatalogError("benign late catalog failure"))
        elif outcome == "unexpected_error":
            jobs[0].set_exception(RuntimeError("benign late failure"))
        else:
            jobs[0].set_result(None)
        for _ in range(10):
            await asyncio.sleep(0)
        jobs.clear()
        gc.collect()
        assert resolver._admission._value == 4
        assert diagnostics == []
        assert wrappers[0]() is None
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.parametrize("returncode, output", [(0, b"0"), (0, b"1x"), (1, b"1")])
def test_fake_worker_protocol_denies_and_closes(monkeypatch, worker_control, returncode, output):
    from nomos.level_0 import tool_schema

    original = subprocess.Popen

    def process(*args, **kwargs):
        child = original(*args, **kwargs)

        def communicate(payload, *, timeout):
            child.returncode = returncode
            return output, None

        child.communicate = communicate
        return child

    monkeypatch.setattr(subprocess, "Popen", process)
    with pytest.raises(tool_schema.ToolSchemaError, match="failed schema validation"):
        tool_schema.validate_tool_arguments({"type": "object"}, {})
    (child,) = worker_control.children
    assert child.stdin.closed and child.stdout.closed
    assert worker_control.releases == 1


@pytest.mark.asyncio
async def test_benign_integer_call_keeps_arguments_and_operation_exact(monkeypatch):
    import asyncio

    from nomos.level_0 import tool_catalog
    from nomos.level_0.types.tool_call import ToolCallRequest
    from nomos.level_0.types.tool_definition import TrustedToolDefinition

    def forbidden(*args, **kwargs):
        pytest.fail("resolution used the default executor")

    monkeypatch.setattr(asyncio, "to_thread", forbidden)
    monkeypatch.setattr(asyncio.get_running_loop(), "run_in_executor", forbidden)
    assert tool_catalog._RESOLVER_EXECUTOR._max_workers == 4
    tool = TrustedToolDefinition(
        name="sample",
        description="A benign integer input",
        usage_constraints=("Use n",),
        parameters={
            "type": "object",
            "properties": {"n": {"type": "integer"}},
            "required": ["n"],
            "additionalProperties": False,
        },
    )
    call = ToolCallRequest(tool_name="sample", arguments={"n": 1}, subject="test-user")
    resolver = tool_catalog.AsyncToolCatalogResolver()
    context = await resolver.resolve(council_id="c", tools=(tool,), tool_call=call)
    assert context.operation == call.to_operation()
    assert context.arguments_json == '{"n":1}'
    bad = ToolCallRequest(tool_name="sample", arguments={"n": "1"}, subject="test-user")
    with pytest.raises(tool_catalog.ToolCatalogError):
        await resolver.resolve(council_id="c", tools=(tool,), tool_call=bad)
    assert bad.arguments == {"n": "1"}
    assert await resolver.resolve(council_id="c", tools=None) is None
    with pytest.raises(tool_catalog.ToolCatalogError):
        await resolver.resolve(council_id="c", tools=(), tool_call=call)
