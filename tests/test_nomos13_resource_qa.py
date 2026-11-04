# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Independent resource acceptance tests, derived only from public contracts."""

import asyncio
import copy
import json
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

REGEX_SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "string", "pattern": "^(a+)+$"}},
    "required": ["value"],
}


def _children(*, include_zombies=False):
    """Count direct children across threads, excluding Python bookkeeping."""
    children = set()
    for entry in Path("/proc/self/task").glob("*/children"):
        try:
            children.update(int(pid) for pid in entry.read_text().split())
        except FileNotFoundError:
            continue
    live = set()
    for pid in children:
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
            state = Path(f"/proc/{pid}/stat").read_text().split(")", 1)[1].split()[0]
            if (include_zombies or state != "Z") and b"multiprocessing.resource_tracker" not in cmd:
                live.add(pid)
        except FileNotFoundError:
            continue
    return live


def _assert_reaped():
    deadline = time.monotonic() + 0.3
    while _children(include_zombies=True) and time.monotonic() < deadline:
        time.sleep(0.01)
    remaining = _children(include_zombies=True)
    assert not remaining, f"validation left live or unreaped children: {remaining}"


def _dag(depth):
    definitions = {"d0": {"type": "string"}}
    for level in range(1, depth + 1):
        ref = {"$ref": f"#/$defs/d{level - 1}"}
        definitions[f"d{level}"] = {"allOf": [ref, copy.deepcopy(ref)]}
    return {
        "type": "object",
        "$defs": definitions,
        "properties": {"value": {"$ref": f"#/$defs/d{depth}"}},
    }


def _validation_case(case):
    from nomos.level_0.tool_schema import (
        ToolSchemaError,
        validate_tool_arguments,
        validate_tool_schema,
    )

    if case == "controls":
        schema = {
            "type": "object",
            "properties": {"value": {"type": "integer"}, "optional": {"default": 7}},
            "required": ["value"],
            "additionalProperties": False,
        }
        arguments = {"value": 4}
        original = copy.deepcopy(arguments)
        validate_tool_schema(schema)
        validate_tool_arguments(schema, arguments)
        assert arguments == original, "validation mutated arguments or inserted a default"
        with pytest.raises(ToolSchemaError):
            validate_tool_arguments(schema, {"value": "4"})
        validate_tool_schema(REGEX_SCHEMA)
        validate_tool_arguments(REGEX_SCHEMA, {"value": "aaaa"})
        local = _dag(4)
        validate_tool_schema(local)
        validate_tool_arguments(local, {"value": "valid"})
        with pytest.raises(ToolSchemaError):
            validate_tool_arguments(local, {"value": 9})
    elif case == "offline":
        schema = {
            "type": "object",
            "properties": {"value": {"$ref": "https://example.invalid/schema.json"}},
        }
        with patch("socket.socket.connect", side_effect=AssertionError("network attempted")):
            with pytest.raises(ToolSchemaError):
                validate_tool_schema(schema)
    elif case in {"regex", "dag"}:
        schema = REGEX_SCHEMA if case == "regex" else _dag(27)
        arguments = {"value": "a" * 29 + "!" if case == "regex" else "x"}
        started = time.monotonic()
        with pytest.raises(ToolSchemaError):
            validate_tool_arguments(schema, arguments)
        elapsed = time.monotonic() - started
        assert elapsed <= 2.8, f"{case} call exceeded 2s deadline plus cleanup: {elapsed:.3f}s"
    elif case == "concurrent":
        barrier = threading.Barrier(8)
        finished = threading.Event()
        peak = [0]

        def monitor():
            while not finished.is_set():
                peak[0] = max(peak[0], len(_children()))
                time.sleep(0.003)

        def hostile_call():
            barrier.wait(timeout=2)
            started = time.monotonic()
            with pytest.raises(ToolSchemaError):
                validate_tool_arguments(REGEX_SCHEMA, {"value": "a" * 29 + "!"})
            return time.monotonic() - started

        observer = threading.Thread(target=monitor, daemon=True)
        observer.start()
        try:
            with ThreadPoolExecutor(max_workers=8) as executor:
                elapsed = list(executor.map(lambda _: hostile_call(), range(8)))
        finally:
            finished.set()
            observer.join(timeout=0.5)
        assert max(elapsed) <= 2.8, f"admission exceeded per-call deadline: {elapsed}"
        assert 1 <= peak[0] <= 4, f"8 hostile calls used {peak[0]} simultaneous workers"
    elif case == "crash":
        result = []

        def call():
            try:
                validate_tool_arguments(REGEX_SCHEMA, {"value": "a" * 29 + "!"})
            except ToolSchemaError:
                result.append("closed")
            except BaseException as error:
                result.append(type(error).__name__)
            else:
                result.append("accepted")

        caller = threading.Thread(target=call, daemon=True)
        caller.start()
        deadline = time.monotonic() + 1.5
        victims = set()
        while caller.is_alive() and time.monotonic() < deadline:
            victims = _children()
            if victims:
                break
            time.sleep(0.005)
        assert victims, "runtime argument validation did not create a terminable child"
        for pid in victims:
            os.kill(pid, signal.SIGKILL)
        caller.join(timeout=2.8)
        assert not caller.is_alive(), "worker crash stranded the validation caller"
        assert result == ["closed"], f"worker crash did not fail closed: {result}"
    else:
        raise AssertionError(f"unknown child scenario: {case}")
    _assert_reaped()


async def _async_case():
    from nomos.config.settings import CouncilSettings, OPAConfig
    from nomos.level_0.tool_catalog import ToolCatalogError
    from nomos.level_0.types.tool_call import ToolCallRequest
    from nomos.nat.functions import AuthorizeInput, CouncilService

    service = CouncilService(
        CouncilSettings(
            council_id="qa",
            tools=[],
            require_auth=False,
            require_council_membership=False,
            council_key_path="",
            opa=OPAConfig(enabled=False),
        )
    )
    request = AuthorizeInput(
        agent_id="a",
        council_id="qa",
        tool_call=ToolCallRequest(tool_name="x", subject="s", arguments={}),
    )
    entered = threading.Event()
    ticks = []

    def slow_resolver(**_kwargs):
        entered.set()
        time.sleep(0.3)
        raise ToolCatalogError("QA deliberate catalog refusal")

    async def heartbeat():
        waiting_until = time.monotonic() + 1
        while not entered.is_set() and time.monotonic() < waiting_until:
            await asyncio.sleep(0.002)
        assert entered.is_set(), "supplied public authorize fixture never reached resolver"
        until = time.monotonic() + 0.2
        while time.monotonic() < until:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

    with patch("nomos.nat.functions.resolve_tool_review_context", side_effect=slow_resolver):
        pulse = asyncio.create_task(heartbeat())
        started = time.monotonic()
        await service.authorize(request)
        completed = time.monotonic()
        await pulse
    during = [tick for tick in ticks if started <= tick < completed]
    assert len(during) >= 5, f"slow catalog validation blocked async heartbeat: {len(during)} ticks"


def _run_isolated(case):
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), case],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        try:
            stdout, stderr = process.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            pytest.fail(f"{case}: public validation exceeded external 8s containment deadline")
        assert process.returncode == 0, f"{case}: isolated check failed\n{stdout}\n{stderr}"
        assert json.loads(stdout.splitlines()[-1]) == {"case": case, "passed": True}
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)


@pytest.mark.parametrize("case", ["regex", "dag"])
def test_hostile_validation_has_wall_deadline_and_reaps_children(case):
    _run_isolated(case)


def test_exact_arguments_and_simple_positive_and_negative_controls():
    _run_isolated("controls")


def test_external_schema_references_never_reach_network():
    _run_isolated("offline")


def test_eight_hostile_calls_share_cap_and_admission_deadline():
    _run_isolated("concurrent")


def test_validation_worker_crash_fails_closed_and_reaps_children():
    _run_isolated("crash")


def test_slow_validation_preserves_authorize_event_loop_responsiveness():
    _run_isolated("async")


if __name__ == "__main__":
    scenario = sys.argv[1]
    if scenario == "async":
        asyncio.run(_async_case())
    else:
        _validation_case(scenario)
    print(json.dumps({"case": scenario, "passed": True}))
