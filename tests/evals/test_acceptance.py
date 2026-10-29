# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
from concurrent.futures import ThreadPoolExecutor

import pytest

from evals.nomos.budget import RequestBudget, SharedCounter
from evals.nomos.grading import grade
from evals.nomos.workflow import attach_candidate, freeze, load, read, run, write
from tests.evals.test_workflow import Backend, spec


def test_shared_budget_counts_failures_and_enforces_parallel_cap(tmp_path):
    budget = RequestBudget.create(tmp_path / "budget.json", calls=3, seconds=60)

    def reserve(_):
        try:
            return budget.reserve("test")
        except RuntimeError:
            return None

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(reserve, range(6)))
    assert sum(x is not None for x in outcomes) == 3
    assert len(read(budget.path)["requests"]) == 3
    with pytest.raises(RuntimeError):
        SharedCounter(budget).__setitem__("calls", 1)


def test_guidance_requires_a_valid_correction_not_a_keyword():
    rule = {
        "environment": "staging",
        "resource_prefix": "/workspace/pilot",
        "commands": ["/usr/bin/true"],
    }
    result = {
        "status": "complete",
        "layer": "council",
        "decision": "deny",
        "guidance": "Use staging",
        "execution": "not_attempted",
    }
    assert not grade("deny", result, rule)["passed"]
    result["correction"] = {
        "target_environment": "staging",
        "target_resource": "/workspace/pilot/check",
        "command": "/usr/bin/true",
    }
    assert grade("deny", result, rule)["passed"]
    result["correction"]["target_resource"] = "/workspace/pilot/../../production"
    assert not grade("deny", result, rule)["passed"]


@pytest.mark.asyncio
async def test_baseline_diagnosis_precedes_immutable_candidate(tmp_path):
    experiment = spec().model_copy(update={"candidate": None})
    write(tmp_path / "spec.json", experiment.model_dump())
    directory = tmp_path / "experiment"
    freeze(tmp_path / "spec.json", directory)
    with pytest.raises(ValueError, match="baseline trials"):
        attach_candidate(directory, "candidate")
    await run(directory, "development", arms=("baseline",), backend_factory=Backend)
    assert read(directory / "state.json")["phase"] == "baseline_measured"
    with pytest.raises(ValueError, match="diagnosis"):
        attach_candidate(directory, "candidate")
    from evals.nomos.feedback import analyze

    await analyze(directory, "development")
    attach_candidate(directory, "candidate")
    assert load(directory)[1].candidate.instruction == "candidate"
    with pytest.raises(ValueError, match="already selected"):
        attach_candidate(directory, "a second candidate")
    await run(directory, "development", arms=("candidate",), backend_factory=Backend)
    assert len(list((directory / "attempts").glob("*.json"))) == 12


@pytest.mark.parametrize(
    "guidance,resource",
    [
        (None, "/workspace/pilot"),
        ("Use staging", None),
        ("Use staging", 42),
        ("", "/workspace/pilot"),
    ],
)
def test_malformed_guidance_is_a_failure_not_a_verifier_exception(guidance, resource):
    rule = {
        "environment": "staging",
        "resource_prefix": "/workspace/pilot",
        "commands": ["/usr/bin/true"],
    }
    result = {
        "status": "complete",
        "layer": "council",
        "decision": "deny",
        "guidance": guidance,
        "execution": "not_attempted",
        "correction": {
            "target_resource": resource,
            "target_environment": "staging",
            "command": "/usr/bin/true",
        },
    }
    assert not grade("deny", result, rule)["passed"]


@pytest.mark.asyncio
async def test_failed_diagnosis_cannot_authorize_candidate_selection(tmp_path):
    experiment = spec().model_copy(update={"candidate": None})
    write(tmp_path / "spec.json", experiment.model_dump())
    directory = tmp_path / "experiment"
    freeze(tmp_path / "spec.json", directory)
    await run(directory, "development", arms=("baseline",), backend_factory=Backend)
    write(directory / "analysis-development/compass.json", {"inference": {"status": "error"}})
    with pytest.raises(ValueError, match="diagnosis failed"):
        attach_candidate(directory, "candidate")


@pytest.mark.asyncio
async def test_nested_provider_usage_is_preserved_without_becoming_a_protocol_error(monkeypatch):
    import httpx

    from evals.nomos.adapter import BoundedBackend
    from nomos.llm.base import LLMMessage, LLMRole

    original = httpx.AsyncClient

    def response(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"decision":"approve","reasoning":"Valid permitted scope"}'
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 5,
                    "total_tokens": 17,
                    "prompt_tokens_details": {"cached_tokens": 0},
                },
            },
        )

    def client(**kwargs):
        return original(transport=httpx.MockTransport(response), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setenv("NOMOS_EVAL_API_KEY", "fixture-key")
    backend = BoundedBackend(spec(), 1)
    result = await backend.complete([LLMMessage(role=LLMRole.USER, content="Fixture request")])
    assert result.usage["total_tokens"] == 17
    assert backend.provider_usage[0]["prompt_tokens_details"] == {"cached_tokens": 0}
    assert not backend.failed


@pytest.mark.asyncio
async def test_two_infrastructure_errors_stop_further_dispatch(tmp_path):
    from evals.nomos.workflow import run
    from tests.evals.test_workflow import frozen

    directory = frozen(tmp_path)
    calls = []

    async def fail(*args, **kwargs):
        calls.append(args[3])
        return {"status": "error", "calls": 1, "decision": None, "layer": "council"}

    await run(directory, "development", executor=fail)
    assert len(calls) == 2
    assert read(directory / "state.json")["stop_reason"] == "infrastructure_error_limit"


def test_scored_feedback_distinguishes_correct_denial_from_missing_correction():
    from evals.nomos.feedback import scored_trace

    task = (
        spec()
        .tasks[1]
        .model_copy(
            update={
                "guidance_rule": {
                    "environment": "staging",
                    "resource_prefix": "/workspace/pilot",
                    "commands": ["/usr/bin/true"],
                }
            }
        )
    )
    row = {
        "trial_id": "observed",
        "chain_id": "fixture",
        "arm": "baseline",
        "seconds": 1,
        "status": "complete",
        "layer": "council",
        "decision": "deny",
        "passed": False,
        "execution": "not_attempted",
        "guidance": "Use staging",
        "correction": None,
        "evidence": [],
    }
    trace = scored_trace(
        row,
        [],
        task,
        [
            {
                "messages": [{"role": "user", "content": "Council review request"}],
                "response": '{"decision":"deny"}',
            }
        ],
    )
    result = trace["evaluator_results"]["nomos.decision-v1"]
    assert result["checks"]["decision_passed"]
    assert not result["checks"]["guidance_passed"]
    assert result["correction_rule"] == task.guidance_rule
    assert trace["root_spans"][0]["attributes"]["subject"] == "council-member-review"


@pytest.mark.asyncio
@pytest.mark.parametrize("delivered", [False, True])
async def test_correction_must_be_delivered_by_supported_council_guidance(tmp_path, delivered):
    import json

    from evals.nomos.adapter import trial
    from nomos.llm.base import LLMResponse

    correction = {
        "command": "/usr/bin/true",
        "target_environment": "staging",
        "target_resource": "/workspace/pilot",
    }

    class CorrectingBackend(Backend):
        async def complete(self, *args, **kwargs):
            return LLMResponse(
                content=json.dumps(
                    {
                        "decision": "deny",
                        "reasoning": "Out of scope",
                        "guidance": json.dumps(
                            {"explanation": "Narrow scope", "correction": correction}
                        )
                        if delivered
                        else "Narrow scope",
                        "correction": correction,
                    }
                )
            )

    result = await trial(
        spec().tasks[1].request,
        "fixture",
        spec(),
        "delivery",
        tmp_path / "trial",
        1,
        backend_factory=CorrectingBackend,
    )
    assert result["status"] == "complete"
    assert result["correction"] == (correction if delivered else None)


def test_provider_configuration_selects_hub_credential_without_exporting_deployment_env(tmp_path):
    from scripts.assurance_optimization_pilot import provider

    env = tmp_path / "settings.env"
    # Only a dummy credential is written for provider selection.
    env.write_text(
        "INFERENCE_HUB_KEY=fixture-only\n"  # pragma: allowlist secret
        "COUNCIL_STORAGE_MODE=postgres\n"  # pragma: allowlist secret
    )
    settings = {
        "model": "nvidia/nvidia/nemotron-3-super-v3",
        "endpoint": "https://inference-api.nvidia.com/v1",
        "credential_variable": "INFERENCE_HUB_KEY",
    }
    config, credential = provider(env, settings=settings)
    assert credential == "fixture-only"
    assert config["model"] == settings["model"]
    assert config["endpoint"] == settings["endpoint"]
    assert "COUNCIL_STORAGE_MODE" not in config


@pytest.mark.asyncio
async def test_offline_freeze_needs_no_provider_credential(tmp_path):
    from scripts.assurance_optimization_pilot import execute

    root = tmp_path / "pilot"
    write(root / "reviewed-experiment.json", spec().model_dump())
    await execute(
        root, tmp_path / "absent.env", tmp_path / "not-needed-python", "not-needed-image", "freeze"
    )
    assert (root / "experiment/manifest.json").exists()


@pytest.mark.asyncio
async def test_manifest_window_overrides_shorter_library_retry_default(tmp_path, monkeypatch):
    import asyncio

    from evals.nomos.adapter import trial
    from nomos.llm.council_agent import CouncilMemberAgent

    monkeypatch.setitem(CouncilMemberAgent.__init__.__kwdefaults__, "retry_budget_seconds", 0.01)

    class SlowBackend(Backend):
        async def complete(self, *args, **kwargs):
            await asyncio.sleep(0.03)
            return await super().complete(*args, **kwargs)

    experiment = spec().model_copy(update={"trial_seconds": 1})
    result = await trial(
        experiment.tasks[0].request,
        "candidate",
        experiment,
        "window",
        tmp_path / "trial",
        1,
        backend_factory=SlowBackend,
    )
    assert result["status"] == "complete"
    assert result["decision"] == "approve"


@pytest.mark.parametrize("trial_seconds", [120, 300])
def test_native_task_agent_window_covers_declared_trial(tmp_path, trial_seconds):
    import tomllib

    from evals.nomos.harbor_bridge import prepare_task

    task = prepare_task(tmp_path, spec().tasks[0], "fixture-image", trial_seconds=trial_seconds)
    config = tomllib.loads((task / "task.toml").read_text())
    assert config["agent"]["timeout_sec"] == trial_seconds + 60
    assert config["agent"]["timeout_sec"] < trial_seconds + 90  # Host cleanup window.


@pytest.mark.asyncio
async def test_native_executor_propagates_long_declared_window(tmp_path, monkeypatch):
    import tomllib

    from evals.nomos.harbor_bridge import HarborExecutor

    experiment = spec().model_copy(update={"trial_seconds": 300})
    experiment.versions["runtime_image"] = "fixture-image"
    monkeypatch.setenv("NOMOS_EVAL_API_KEY", "fixture-only")
    directory = tmp_path / "native"

    async def inspect_dispatch(*args, **kwargs):
        task = directory / "task/task.toml"
        assert tomllib.loads(task.read_text())["agent"]["timeout_sec"] == 360
        assert read(directory / "input.json")["settings"]["trial_seconds"] == 300
        raise RuntimeError("inspected native dispatch")

    monkeypatch.setattr("asyncio.create_subprocess_exec", inspect_dispatch)
    executor = HarborExecutor(
        python=tmp_path / "harbor-python",
        image="fixture-image",
        budget=RequestBudget.create(tmp_path / "budget.json", calls=1, seconds=60),
    )
    with pytest.raises(RuntimeError, match="inspected native dispatch"):
        await executor(experiment.tasks[0].request, "baseline", experiment, "trial", directory, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("stalled", [False, True])
async def test_workflow_bounds_native_overhead_without_truncating_worker(
    tmp_path, monkeypatch, stalled
):
    import asyncio

    from evals.nomos.harbor_bridge import HarborExecutor

    experiment = spec().model_copy(update={"trial_seconds": 1})
    write(tmp_path / "spec.json", experiment.model_dump())
    directory = tmp_path / "experiment"
    freeze(tmp_path / "spec.json", directory)
    actual_wait_for = asyncio.wait_for
    cancelled = []

    async def scaled_wait_for(awaitable, timeout):
        # Speed up the clock: 1s worker plus 120s native allowance becomes .121s.
        assert timeout == 121
        return await actual_wait_for(awaitable, timeout=timeout / 1000)

    class NativeFixture:
        lifecycle_overhead_seconds = HarborExecutor.lifecycle_overhead_seconds

        async def __call__(self, request, *args, **kwargs):
            try:
                # Setup outlasts the worker window; normal worker remains within it.
                await asyncio.sleep(0.2 if stalled else 0.02)
                await asyncio.sleep(0.0005)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
            return {
                "status": "complete",
                "calls": 1,
                "layer": "council",
                "decision": "approve" if request["target_environment"] == "staging" else "deny",
            }

    monkeypatch.setattr(asyncio, "wait_for", scaled_wait_for)
    await run(directory, "development", arms=("baseline",), executor=NativeFixture())
    rows = [read(p) for p in (directory / "attempts").glob("*.json")]
    if stalled:
        assert len(rows) == len(cancelled) == 1
        assert all(r["status"] == "error" and r["error"] == "TimeoutError" for r in rows)
        # Unknown interrupted usage conservatively consumes the remaining call reservation.
        assert read(directory / "state.json")["stop_reason"] == "budget_exhausted"
    else:
        assert len(rows) == 6 and not cancelled
        assert all(r["status"] == "complete" and r["passed"] for r in rows)


@pytest.mark.asyncio
async def test_workflow_reserves_native_overhead_before_dispatch(tmp_path):
    from evals.nomos.harbor_bridge import HarborExecutor

    experiment = spec().model_copy(update={"trial_seconds": 1, "total_seconds": 120})
    write(tmp_path / "spec.json", experiment.model_dump())
    directory = tmp_path / "experiment"
    freeze(tmp_path / "spec.json", directory)

    class NoDispatch:
        lifecycle_overhead_seconds = HarborExecutor.lifecycle_overhead_seconds

        async def __call__(self, *args, **kwargs):
            raise AssertionError("budget cannot accommodate native lifecycle")

    await run(directory, "development", arms=("baseline",), executor=NoDispatch())
    assert not list((directory / "attempts").glob("*.json"))
    assert read(directory / "state.json")["stop_reason"] == "budget_exhausted"
