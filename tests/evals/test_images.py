# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Docker runtime identity checks run before freezing or reserving provider requests."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from evals.nomos import images
from evals.nomos.budget import RequestBudget
from evals.nomos.fixtures import spec
from evals.nomos.harbor_bridge import HarborExecutor
from evals.nomos.workflow import freeze, read, write

IMAGE = "sha256:" + "a" * 64


def inspected(**changes):
    return {
        "Id": IMAGE,
        "Os": "linux",
        "Architecture": "amd64",
        "Descriptor": {"mediaType": "application/vnd.oci.image.manifest.v1+json"},
        **changes,
    }


def docker(monkeypatch, value):
    def run(command, **options):
        assert command == ["docker", "image", "inspect", IMAGE]
        assert options["timeout"] == 10
        assert options["check"] is True
        return SimpleNamespace(stdout=json.dumps([value]))

    monkeypatch.setattr(images.subprocess, "run", run)


@pytest.mark.parametrize(
    "descriptor",
    [
        None,
        {"mediaType": "application/vnd.oci.image.manifest.v1+json"},
        {"mediaType": "application/vnd.docker.distribution.manifest.v2+json"},
    ],
)
def test_inspect_accepts_single_platform_images_and_legacy_docker(monkeypatch, descriptor):
    value = inspected(Variant="v8")
    if descriptor is None:
        del value["Descriptor"]
    else:
        value["Descriptor"] = descriptor
    docker(monkeypatch, value)
    assert images.inspect_image(IMAGE) == "linux/amd64/v8"


@pytest.mark.parametrize(
    "value",
    [
        inspected(Id="sha256:" + "b" * 64),
        inspected(Descriptor={"mediaType": "application/vnd.oci.image.index.v1+json"}),
        inspected(
            Descriptor={"mediaType": "application/vnd.docker.distribution.manifest.list.v2+json"}
        ),
        inspected(Architecture=""),
        inspected(Os=None),
    ],
)
def test_inspect_rejects_indexes_aliases_and_missing_platform(monkeypatch, value):
    docker(monkeypatch, value)
    with pytest.raises(ValueError):
        images.inspect_image(IMAGE)


def test_freeze_rejects_index_before_creating_experiment(tmp_path, monkeypatch):
    docker(
        monkeypatch, inspected(Descriptor={"mediaType": "application/vnd.oci.image.index.v1+json"})
    )
    value = spec().model_dump()
    value["versions"]["runtime_image"] = IMAGE
    write(tmp_path / "reviewed.json", value)
    with pytest.raises(ValueError, match="single-platform"):
        freeze(tmp_path / "reviewed.json", tmp_path / "experiment")
    assert not (tmp_path / "experiment").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["platform", "index", "unavailable"])
async def test_native_checks_identity_before_each_reservation(tmp_path, monkeypatch, failure):
    experiment = spec()
    experiment.versions.update(runtime_image=IMAGE, runtime_platform="linux/amd64")
    budget = RequestBudget.create(tmp_path / "budget.json", calls=2, seconds=60)
    executor = HarborExecutor(python=tmp_path / "python", image=IMAGE, budget=budget)
    calls = []

    def inspect(image):
        calls.append(image)
        if len(calls) == 1:
            return "linux/amd64"
        if failure == "platform":
            return "linux/arm64"
        if failure == "index":
            raise ValueError("single-platform image required")
        raise subprocess.CalledProcessError(1, ["docker", "image", "inspect"])

    monkeypatch.setattr(images, "inspect_image", inspect)

    # Stop the first trial immediately after its reservation, without Docker/model effects.
    def reserved(purpose):
        RequestBudget(tmp_path / "budget.json").reserve(purpose)
        raise RuntimeError("first reservation observed")

    monkeypatch.setattr(budget, "reserve", reserved)
    with pytest.raises(RuntimeError, match="first reservation"):
        await executor(
            experiment.tasks[0].request, "baseline", experiment, "one", tmp_path / "one", 1
        )
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        await executor(
            experiment.tasks[0].request, "candidate", experiment, "two", tmp_path / "two", 1
        )
    assert calls == [IMAGE, IMAGE]
    assert len(read(tmp_path / "budget.json")["requests"]) == 1
    assert not (tmp_path / "two").exists()
