# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Failure and isolation contracts for the offline fixture scripts."""

import hashlib
from pathlib import Path

import pytest

from evals.nomos.fixtures import spec
from evals.nomos.grading import grade
from evals.nomos.workflow import read, write
from scripts import assurance_authoring_example as authoring
from scripts import assurance_optimization_controls as controls


@pytest.mark.asyncio
async def test_authoring_pins_storage_and_signer(tmp_path, monkeypatch):
    monkeypatch.setenv("COUNCIL_STORAGE_MODE", "postgres")
    monkeypatch.setenv("COUNCIL_DATABASE_URL", "postgresql://deployed.invalid/db")
    monkeypatch.setenv("NOMOS_SIGNER_PROVIDER", "vault")
    observed = []

    def service(settings):
        observed.append(settings)
        raise RuntimeError("stop before council construction")

    monkeypatch.setattr(authoring, "CouncilService", service)
    with pytest.raises(RuntimeError, match="stop before"):
        await authoring.capture(tmp_path / "pilot")
    assert observed[0].storage.mode == "redis"
    assert observed[0].storage.dsn == ""
    assert observed[0].signing.provider == "local"


def controls_spec(tmp_path):
    experiment = spec()
    experiment.proofs["verifier_sha256"] = hashlib.sha256(
        Path("evals/nomos/grading.py").read_bytes()
    ).hexdigest()
    source = tmp_path / "spec.json"
    write(source, experiment.model_dump())
    return source


@pytest.mark.asyncio
async def test_controls_accept_tasks_without_guidance(tmp_path, monkeypatch):
    source = controls_spec(tmp_path)

    async def spawn(*args, **kwargs):
        task_dir, directory, answer = map(Path, args[3:6])
        expected = read(task_dir / "tests/expected.json")
        reward = int(grade(expected["expected"], read(answer), expected["rule"])["passed"])
        write(
            directory / "driver-result.json", {"verifier_result": {"rewards": {"reward": reward}}}
        )

        class Process:
            async def wait(self):
                return 0

        return Process()

    monkeypatch.setattr(controls.asyncio, "create_subprocess_exec", spawn)
    results = await controls.controls(source, tmp_path / "controls", Path("python"), "fixture")
    assert len(results) == 24
    assert all(r["passed"] for r in results)


@pytest.mark.parametrize("kill_needed", [False, True])
@pytest.mark.asyncio
async def test_controls_reap_timed_out_driver(tmp_path, monkeypatch, kill_needed):
    source = controls_spec(tmp_path)
    calls = []

    class Process:
        returncode = None

        async def wait(self):
            calls.append("wait")
            if calls.count("wait") <= (2 if kill_needed else 1):
                raise TimeoutError
            self.returncode = -9 if kill_needed else -15
            return self.returncode

        def terminate(self):
            calls.append("terminate")

        def kill(self):
            calls.append("kill")

    async def spawn(*args, **kwargs):
        return Process()

    monkeypatch.setattr(controls.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(TimeoutError):
        await controls.controls(source, tmp_path / "controls", Path("python"), "fixture")
    assert calls == (
        ["wait", "terminate", "wait", "kill", "wait"]
        if kill_needed
        else ["wait", "terminate", "wait"]
    )


@pytest.mark.asyncio
async def test_authoring_rejects_deployment_public_key_export(tmp_path, monkeypatch):
    export = tmp_path / "deployed-public-key.json"
    original = b'{"public_key": "existing-deployment-key"}\n'
    export.write_bytes(original)
    monkeypatch.setenv("COUNCIL_PUBLIC_KEY_EXPORT_PATH", str(export))

    def service(settings):
        pytest.fail("fixture must reject deployment export before constructing a signer")

    monkeypatch.setattr(authoring, "CouncilService", service)
    with pytest.raises(ValueError, match="COUNCIL_PUBLIC_KEY_EXPORT_PATH"):
        await authoring.capture(tmp_path / "pilot")
    assert export.read_bytes() == original
    assert not (tmp_path / "pilot").exists()
