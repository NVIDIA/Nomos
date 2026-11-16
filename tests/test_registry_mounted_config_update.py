# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""A catalog on a Kubernetes ConfigMap mount survives the mount being updated.

A ConfigMap volume is a set of symlinks: ``catalog.yaml -> ..data/catalog.yaml``
and ``..data -> ..<timestamp>``. An update writes a new timestamped directory,
repoints ``..data`` and deletes the old directory. A path that was resolved
through the symlinks names the deleted directory from then on.
"""

import os
import shutil

import pytest
import yaml

pytest.importorskip("nat", reason="nvidia-nat not installed")

import nomos.nat.registry as registry_module
from nomos.config.settings import load_council_catalog
from nomos.nat.registry import get_registry
from tests.test_council_registry import _write_catalog
from tests.test_registry_lifecycle import FakeService


def _mount(root, name, version, content):
    """Write `content` as a new version of the mount and switch to it, as the kubelet does."""
    directory = root / f"..{version}"
    directory.mkdir()
    (directory / name).write_text(content)
    staged = root / "..data_tmp"
    os.symlink(directory.name, staged)
    previous = os.readlink(root / "..data") if (root / "..data").is_symlink() else None
    os.replace(staged, root / "..data")
    if not (root / name).is_symlink():
        os.symlink(f"..data/{name}", root / name)
    if previous:
        shutil.rmtree(root / previous)
    return root / name


@pytest.fixture
def mounted(tmp_path, monkeypatch):
    FakeService.instances = []
    FakeService.init_release = FakeService.init_entered = None
    monkeypatch.setattr("nomos.nat.registry.CouncilService", FakeService)
    monkeypatch.setattr(registry_module, "_registries", {})
    source = _write_catalog(tmp_path)
    root = tmp_path / "bootstrap-config"
    root.mkdir()
    return root, _mount(root, "catalog.yaml", "v1", source.read_text())


@pytest.mark.asyncio
async def test_registry_reads_the_new_catalog_after_the_mount_is_updated(mounted):
    root, path = mounted
    registry = await get_registry(str(path))
    assert set(registry.council_ids) == {"council-a", "council-b"}

    changed = yaml.safe_load(path.read_text())
    changed["councils"]["council-c"] = {}
    _mount(root, "catalog.yaml", "v2", yaml.safe_dump(changed))

    # The directory the first read came from is gone; the mounted path is not.
    assert not (root / "..v1").exists()
    assert await registry.reload_if_changed() is True
    assert set(registry.council_ids) == {"council-a", "council-b", "council-c"}
    await registry.aclose()


@pytest.mark.asyncio
async def test_an_identical_remount_is_not_a_change(mounted):
    root, path = mounted
    registry = await get_registry(str(path))
    _mount(root, "catalog.yaml", "v2", path.read_text())

    assert await registry.reload_if_changed() is False
    await registry.aclose()


def test_referenced_profiles_are_found_through_the_mount_after_an_update(tmp_path):
    root = tmp_path / "bootstrap-config"
    root.mkdir()
    profile = yaml.safe_load(_write_catalog(tmp_path).read_text())["shared"]
    profile["council_id"] = "council-a"
    for version in ("v1", "v2"):
        directory = root / f"..{version}"
        directory.mkdir()
        (directory / "council.yaml").write_text(yaml.safe_dump(profile))
        (directory / "catalog.yaml").write_text(
            yaml.safe_dump(
                {
                    "default_council_id": "council-a",
                    "councils": {"council-a": {"config_file": "council.yaml"}},
                }
            )
        )
    os.symlink("..v1", root / "..data")
    for name in ("catalog.yaml", "council.yaml"):
        os.symlink(f"..data/{name}", root / name)

    assert registry_module._catalog_fingerprint(root / "catalog.yaml")
    os.replace(_staged(root, "..v2"), root / "..data")
    shutil.rmtree(root / "..v1")

    assert registry_module._catalog_fingerprint(root / "catalog.yaml")
    assert load_council_catalog(str(root / "catalog.yaml")).default_council_id == "council-a"


def _staged(root, target):
    staged = root / "..data_tmp"
    os.symlink(target, staged)
    return staged
