# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Exercise CLI dispatch through real frozen workflow/accounting with local answers."""

import json
import os
import sys

import pytest

from evals.nomos import __main__ as cli
from evals.nomos.budget import RequestBudget
from evals.nomos.fixtures import spec
from evals.nomos.workflow import freeze, read, write


@pytest.fixture(autouse=True)
def restore_umask():
    original = os.umask(0o077)
    yield
    os.umask(original)


def invoke(monkeypatch, *arguments):
    monkeypatch.setattr(sys, "argv", ["nomos-eval", *map(str, arguments)])
    cli.main()


def frozen(tmp_path):
    experiment = spec()
    experiment.candidate = None
    experiment.versions["runtime_image"] = "sha256:reviewed-image"
    write(tmp_path / "reviewed.json", experiment.model_dump())
    directory = tmp_path / "experiment"
    freeze(tmp_path / "reviewed.json", directory)
    return directory


def test_create_budget_refuses_reset(tmp_path, monkeypatch):
    path = tmp_path / "budget.json"
    invoke(monkeypatch, "create-budget", path, "--calls", 24, "--seconds", 3600)
    RequestBudget(path).reserve("previous-attempt")
    original = path.read_bytes()
    with pytest.raises(ValueError, match="existing"):
        invoke(monkeypatch, "create-budget", path, "--calls", 48, "--seconds", 7200)
    assert path.read_bytes() == original
    assert path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("mode", ["complete", "error", "budget"])
def test_harbor_dispatch_and_incomplete_exit(tmp_path, monkeypatch, capsys, mode):
    from evals.nomos import harbor_bridge

    directory = frozen(tmp_path)
    budget = tmp_path / "budget.json"
    RequestBudget.create(budget, calls=1 if mode == "budget" else 6, seconds=3600)
    observed = []
    interpreter = tmp_path / "harbor-python"
    interpreter.symlink_to(sys.executable)

    class Executor:
        max_calls_per_trial = 1
        lifecycle_overhead_seconds = 120

        def __init__(self, *, python, image, budget):
            assert python == interpreter.absolute()
            assert image == "sha256:reviewed-image"
            self.budget = budget

        async def __call__(self, request, instruction, spec, *args, **kwargs):
            self.budget.reserve("council:local-test")
            observed.append((request, instruction))
            if mode == "error":
                raise RuntimeError("local infrastructure error")
            return {"status": "complete", "layer": "council", "decision": "deny", "calls": 1}

    monkeypatch.setattr(harbor_bridge, "HarborExecutor", Executor)
    arguments = (
        "run",
        directory,
        "--executor",
        "harbor",
        "--harbor-python",
        interpreter,
        "--image",
        "sha256:reviewed-image",
        "--budget",
        budget,
        "--arm",
        "baseline",
        "--split",
        "development",
    )
    if mode == "complete":
        invoke(monkeypatch, *arguments)
    else:
        with pytest.raises(SystemExit) as error:
            invoke(monkeypatch, *arguments)
        assert error.value.code == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["executor"] == "harbor"
    assert summary["expected"] == 6
    assert summary["completed"] == (6 if mode == "complete" else 1 if mode == "budget" else 0)
    assert len(read(budget)["requests"]) == (
        6 if mode == "complete" else 1 if mode == "budget" else 2
    )
    before = sorted(p.read_bytes() for p in (directory / "attempts").glob("*.json"))
    if mode == "complete":
        # Resume must not dispatch or reserve again. Wrong answers still completed.
        invoke(monkeypatch, *arguments)
        assert len(observed) == 6
        assert any(not read(p)["passed"] for p in (directory / "attempts").glob("*.json"))
    else:
        assert summary["stop_reason"] == "infrastructure_error_limit"
    assert before == sorted(p.read_bytes() for p in (directory / "attempts").glob("*.json"))


@pytest.mark.parametrize(
    "options",
    [
        ["--executor", "harbor"],
        ["--budget", "unused.json"],
        [
            "--executor",
            "harbor",
            "--harbor-python",
            "/missing/python",
            "--image",
            "x",
            "--budget",
            "x",
        ],
    ],
)
def test_invalid_execution_options_fail_before_attempts(tmp_path, monkeypatch, options):
    directory = frozen(tmp_path)
    with pytest.raises(SystemExit) as error:
        invoke(monkeypatch, "run", directory, *options)
    assert error.value.code == 2
    assert not (directory / "attempts").exists()


def test_harbor_rejects_image_mismatch_before_reserving(tmp_path, monkeypatch):
    directory = frozen(tmp_path)
    path = tmp_path / "budget.json"
    RequestBudget.create(path)
    with pytest.raises(SystemExit) as error:
        invoke(
            monkeypatch,
            "run",
            directory,
            "--executor",
            "harbor",
            "--harbor-python",
            sys.executable,
            "--image",
            "different",
            "--budget",
            path,
        )
    assert error.value.code == 2
    assert read(path)["requests"] == []
    assert not (directory / "attempts").exists()


def test_diagnosis_uses_existing_shared_budget(tmp_path, monkeypatch):
    from evals.nomos import feedback

    path = tmp_path / "budget.json"
    RequestBudget.create(path, calls=2, seconds=3600)
    RequestBudget(path).reserve("baseline")
    config = tmp_path / "judge.json"
    write(
        config,
        {"model": "openai/fixture", "api_base": "https://example.invalid/v1", "max_calls": 1},
    )
    monkeypatch.setenv("NOMOS_EVAL_ANALYSIS_API_KEY", "test-credential")

    async def analyze(directory, split, *, inference, shared_budget):
        assert split == "development"
        assert inference.max_calls == 1
        shared_budget.reserve("diagnosis")

    monkeypatch.setattr(feedback, "analyze", analyze)
    invoke(monkeypatch, "analyze", tmp_path, "--inference-config", config, "--budget", path)
    assert [r["purpose"] for r in read(path)["requests"]] == ["baseline", "diagnosis"]


def test_direct_default_keeps_in_process_executor(tmp_path, monkeypatch, capsys):
    from evals.nomos import adapter

    directory = frozen(tmp_path)

    async def answer(*args, **kwargs):
        return {"status": "complete", "layer": "council", "decision": "deny", "calls": 1}

    monkeypatch.setattr(adapter, "trial", answer)
    invoke(monkeypatch, "run", directory, "--arm", "baseline")
    assert json.loads(capsys.readouterr().out)["executor"] == "direct"
