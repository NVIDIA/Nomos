# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
import asyncio
import io
import json
import zipfile

import pytest

from assurance.analysis import digest
from assurance.author_export import build_capture, resolve_selection
from assurance.monitor import Store
from evals.nomos.adapter import trial
from evals.nomos.feedback import scored_trace
from evals.nomos.fixtures import spec
from evals.nomos.workflow import (
    Experiment,
    compare,
    freeze,
    freeze_candidate,
    intake,
    load,
    read,
    run,
    write,
)
from nomos.llm.base import LLMBackend, LLMResponse


class Backend(LLMBackend):
    def provider_name(self):
        return "fixture"

    async def complete(self, messages, *args, **kwargs):
        candidate = "candidate" in messages[0].content
        decision = "approve" if "staging" in messages[-1].content else "deny"
        if not candidate:
            decision = "deny"
        return LLMResponse(content=json.dumps({"decision": decision, "reasoning": "fixture"}))


def frozen(tmp_path):
    path = tmp_path / "spec.json"
    write(path, spec().model_dump())
    destination = tmp_path / "experiment"
    freeze(path, destination)
    return destination


@pytest.mark.asyncio
async def test_real_council_isolation_and_score_mapping(tmp_path):
    experiment = spec()
    first = await trial(
        experiment.tasks[0].request,
        "candidate",
        experiment,
        "first",
        tmp_path / "first",
        3,
        backend_factory=Backend,
    )
    second = await trial(
        experiment.tasks[1].request,
        "candidate",
        experiment,
        "second",
        tmp_path / "second",
        3,
        backend_factory=Backend,
    )
    assert first["decision"] == "approve"
    assert second["decision"] == "deny"
    assert first["intent_id"] != second["intent_id"]
    assert first["chain_id"] != second["chain_id"]
    assert first["status"] == second["status"] == "complete"
    assert first["calls"] == 1
    row = dict(first, trial_id="first", seconds=1, passed=True, arm="candidate")
    trace = scored_trace(row, read(tmp_path / "first/verified-ledger.json"), experiment.tasks[0])
    assert trace["evaluator_results"]["nomos.decision-v1"]["score"] == 1


@pytest.mark.asyncio
async def test_run_resume_freeze_and_final_gates(tmp_path):
    directory = frozen(tmp_path)
    with pytest.raises(ValueError, match="freeze candidate"):
        await run(directory, "held-out", backend_factory=Backend)
    await run(directory, "development", backend_factory=Backend)
    assert compare(directory, "development")["decision"] == "accepted"
    before = {p.name: p.read_bytes() for p in (directory / "attempts").glob("*")}
    await run(directory, "development", backend_factory=Backend)
    assert before == {p.name: p.read_bytes() for p in (directory / "attempts").glob("*")}
    design = read(directory / "design.json")
    assert all(t["split"] == "development" for t in design["tasks"])
    freeze_candidate(directory)
    await run(directory, "held-out", backend_factory=Backend)
    assert compare(directory, "held-out")["decision"] == "accepted"
    with pytest.raises(ValueError, match="candidate is frozen"):
        await run(directory, "development", backend_factory=Backend)


def test_freeze_detects_changed_contract_and_stale_proofs(tmp_path):
    directory = frozen(tmp_path)
    value = read(directory / "manifest.json")
    value["contract"] = "Changed"
    write(directory / "manifest.json", value)
    with pytest.raises(ValueError, match="identity"):
        load(directory)
    value = spec().model_dump()
    value["tasks"][0]["request"]["command"] = "/usr/bin/false"
    write(tmp_path / "bad.json", value)
    with pytest.raises(ValueError, match="proof"):
        freeze(tmp_path / "bad.json", tmp_path / "bad")


def test_split_family_and_controls():
    value = spec().model_dump()
    value["tasks"][-1]["family"] = value["tasks"][0]["family"]
    with pytest.raises(ValueError, match="family"):
        Experiment.model_validate(value)
    controls = spec().proofs["controls"]
    assert all(
        c["oracle"] and not c["nop"] and not c["targeted_failure"] for c in controls.values()
    )
    assert not controls["dev-safe"]["deny_all"]
    assert not controls["dev-unsafe"]["approve_all"]


def test_missing_errors_and_must_pass_cannot_hide_in_aggregate(tmp_path):
    directory = frozen(tmp_path)
    assert compare(directory, "development")["decision"] == "inconclusive"
    for task in spec().tasks[:2]:
        for arm in ("baseline", "candidate"):
            for i in range(3):
                write(
                    directory / "attempts" / f"{task.id}.{arm}.{i}.json",
                    {
                        "trial_key": f"{task.id}.{arm}.{i}",
                        "instruction_sha256": digest(getattr(spec(), arm).model_dump()),
                        "experiment_id": read(directory / "manifest.json")["experiment_id"],
                        "trial_id": f"{task.id}.{arm}.{i}",
                        "split": task.split,
                        "repetition": i,
                        "calls": 1,
                        "seconds": 1,
                        "task_id": task.id,
                        "arm": arm,
                        "decision": "approve",
                        "status": "complete",
                        "layer": "council",
                    },
                )
    report = compare(directory, "development")
    assert report["decision"] == "rejected"
    assert len(report["unsafe_approvals"]) == 3


@pytest.mark.asyncio
async def test_interrupt_reservation_survives_resume(tmp_path, monkeypatch):
    from evals.nomos import adapter

    directory = frozen(tmp_path)

    async def interrupt(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(adapter, "trial", interrupt)
    with pytest.raises(asyncio.CancelledError):
        await run(directory, "development", backend_factory=Backend)
    files = list((directory / "attempts").glob("*.json"))
    assert len(files) == 1
    row = read(files[0])
    assert row["status"] == "interrupted" and row["calls"] == spec().max_calls
    await run(directory, "development", backend_factory=Backend)
    assert len(list((directory / "attempts").glob("*.json"))) == 1
    assert compare(directory, "development")["decision"] == "inconclusive"


def test_intake_integrity_and_partial_accounting(tmp_path):
    store = Store(tmp_path / "observer.db", "chain")
    store.record_incident(
        {"id": "finding", "kind": "repeated_unchanged_denial", "evidence": [], "agent_id": "test"}
    )
    bundle, _ = build_capture(resolve_selection(store, ["finding"]), "council-decision", [])
    archive = tmp_path / "capture.zip"
    archive.write_bytes(bundle)
    receipt = intake(archive, tmp_path / "intake")
    assert receipt["findings"][0]["disposition"] == "deferred"
    assert receipt["completeness"][0]["missing_inputs"]
    with zipfile.ZipFile(io.BytesIO(bundle)) as source:
        files = {n: source.read(n) for n in source.namelist()}
    files["findings.json"] = b"[]"
    with zipfile.ZipFile(archive, "w") as target:
        for name, value in files.items():
            target.writestr(name, value)
    with pytest.raises(ValueError, match="hash"):
        intake(archive, tmp_path / "bad")


@pytest.mark.asyncio
async def test_ambient_deployment_configuration_is_refused_before_effects(tmp_path, monkeypatch):
    target = tmp_path / "deployment-public-key"
    target.write_text("existing deployment key")
    monkeypatch.setenv("COUNCIL_PUBLIC_KEY_EXPORT_PATH", str(target))
    experiment = spec()
    with pytest.raises(ValueError, match="clean evaluation environment"):
        await trial(
            experiment.tasks[0].request,
            "candidate",
            experiment,
            "isolated",
            tmp_path / "trial",
            1,
            backend_factory=Backend,
        )
    assert target.read_text() == "existing deployment key"
    assert not (tmp_path / "trial").exists()


@pytest.mark.asyncio
async def test_resume_rejects_foreign_trial_accounting(tmp_path):
    directory = frozen(tmp_path)
    await run(directory, "development", backend_factory=Backend)
    row_path = next((directory / "attempts").glob("*.json"))
    row = read(row_path)
    row["experiment_id"] = "another-experiment"
    write(row_path, row)
    with pytest.raises(ValueError, match="identity mismatch"):
        await run(directory, "development", backend_factory=Backend)
    with pytest.raises(ValueError, match="identity mismatch"):
        compare(directory, "development")


@pytest.mark.parametrize("reasoning", [None, "", "n/a", "see above"])
@pytest.mark.asyncio
async def test_empty_reasoning_is_an_error_even_for_expected_denial(tmp_path, reasoning):
    class MalformedBackend(Backend):
        async def complete(self, *args, **kwargs):
            return LLMResponse(content=json.dumps({"decision": "deny", "reasoning": reasoning}))

    experiment = spec()
    result = await trial(
        experiment.tasks[1].request,
        "candidate",
        experiment,
        "malformed",
        tmp_path / "trial",
        1,
        backend_factory=MalformedBackend,
    )
    assert result["status"] == "error"
    assert result["calls"] == 1
    assert (
        json.loads(read(tmp_path / "trial/interactions.json")[0]["response"])["reasoning"]
        == reasoning
    )


@pytest.mark.asyncio
async def test_actual_compass_input_contains_independent_scores(tmp_path, monkeypatch):
    from insight_agent.compass_signals.tool_issues.signal import ToolIssueCompassSignal

    from evals.nomos.feedback import analyze

    directory = frozen(tmp_path)
    await run(directory, "development", backend_factory=Backend)
    seen = []
    original = ToolIssueCompassSignal.check_prerequisites

    def inspect(self, snapshot):
        seen.extend(snapshot)
        return original(self, snapshot)

    monkeypatch.setattr(ToolIssueCompassSignal, "check_prerequisites", inspect)
    await analyze(directory, "development")
    assert len(seen) == 12
    assert {t.attributes["arm"] for t in seen} == {"baseline", "candidate"}
    assert {t.evaluator_results["nomos.decision-v1"]["score"] for t in seen} == {0, 1}
    assert all(t.attributes["task_id"] and t.attributes["evidence"] for t in seen)


@pytest.mark.asyncio
async def test_vault_deployment_signer_cannot_replace_local_trial_keys(tmp_path, monkeypatch):
    import nomos.nat.functions as functions

    monkeypatch.setenv("NOMOS_SIGNER_PROVIDER", "vault")
    monkeypatch.setenv("NOMOS_VAULT_ADDR", "https://example.invalid")

    def no_vault(*args, **kwargs):
        raise AssertionError("trial must not construct a deployment Vault signer")

    monkeypatch.setattr(functions, "VaultTransitSigner", no_vault)
    experiment = spec()
    result = await trial(
        experiment.tasks[0].request,
        "candidate",
        experiment,
        "local",
        tmp_path / "local",
        1,
        backend_factory=Backend,
    )
    assert result["status"] == "complete"
    assert (tmp_path / "local/keys").is_dir()


@pytest.mark.asyncio
async def test_deployment_database_is_rejected_before_startup(tmp_path, monkeypatch):
    monkeypatch.setenv("COUNCIL_STORAGE_MODE", "postgres")
    monkeypatch.setenv("COUNCIL_DATABASE_URL", "postgresql://example.invalid/deployment")
    experiment = spec()
    with pytest.raises(ValueError, match="clean evaluation environment"):
        await trial(
            experiment.tasks[0].request,
            "candidate",
            experiment,
            "db",
            tmp_path / "db",
            1,
            backend_factory=Backend,
        )
    assert not (tmp_path / "db").exists()


@pytest.mark.asyncio
async def test_scored_compass_inference_uses_frozen_contract_and_budget(tmp_path, monkeypatch):
    from insight_agent.compass_signals.compass_signals import CompassSignalResult
    from insight_agent.compass_signals.grader_failure_patterns import (
        GraderFailurePatternsCompassSignal,
    )

    import assurance.ethos as ethos
    from evals.nomos.feedback import analyze

    directory = frozen(tmp_path)
    await run(directory, "development", backend_factory=Backend)
    observed = {}

    def build(inference, budget):
        observed["budget"] = budget
        return object()

    async def grade(self, snapshot):
        observed["scores"] = [t.evaluator_results for t in snapshot]
        return CompassSignalResult(signal_name="grader-failure-patterns", problems=())

    async def contract(snapshot, text, inference, budget):
        assert budget is observed["budget"]
        observed["contract"] = text
        return CompassSignalResult(signal_name="ethos-divergence", problems=())

    monkeypatch.setattr(ethos, "_build_llm", build)
    monkeypatch.setattr(ethos, "_detect", contract)
    monkeypatch.setattr(GraderFailurePatternsCompassSignal, "analyze", grade)
    inference = ethos.InferenceConfig(
        model="openai/fixture",
        api_base="https://example.invalid/v1",
        api_key="test-only",  # pragma: allowlist secret
        max_calls=2,
    )
    await analyze(directory, "development", inference=inference)
    assert len(observed["scores"]) == 12
    assert observed["contract"] == spec().contract


@pytest.mark.parametrize("bounded", [True, False])
@pytest.mark.parametrize("error", [RuntimeError, TimeoutError])
@pytest.mark.asyncio
async def test_executor_errors_charge_declared_ceiling(tmp_path, bounded, error):
    directory = frozen(tmp_path)

    class FailingExecutor:
        async def __call__(self, *args, **kwargs):
            raise error("native infrastructure failure")

    executor = FailingExecutor()
    if bounded:
        executor.max_calls_per_trial = 1
    await run(directory, "development", executor=executor)
    rows = [read(p) for p in (directory / "attempts").glob("*.json")]
    assert len(rows) == (spec().max_infrastructure_errors if bounded else 1)
    assert all(r["calls"] == (1 if bounded else spec().max_calls) for r in rows)
    assert read(directory / "state.json")["stop_reason"] == (
        "infrastructure_error_limit" if bounded else "budget_exhausted"
    )
