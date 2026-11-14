# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""A route checks current config once without weakening reload or lease safety."""

import asyncio

import pytest
import yaml

import nomos.nat.registry as registry_module
from tests.test_registry_lifecycle import FakeService, _change, _request
from tests.test_registry_lifecycle import registry as registry


async def select(registry, operation):
    if operation == "get":
        return await registry.get("council-a")
    method = getattr(registry, "service_for_" + operation)
    service, normalized = await method(_request(operation == "authorize"))
    assert normalized.council_id == "council-a"
    return service


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get", "authorize", "propose"])
async def test_one_fingerprint_per_routed_selection(registry, monkeypatch, operation):
    original = registry_module._catalog_fingerprint
    calls = []

    def counted(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(registry_module, "_catalog_fingerprint", counted)
    async with registry.request_scope():
        first = await select(registry, operation)
        assert calls == [registry._catalog_path]
        assert registry._lease_counts[first] == 1
        calls.clear()
        assert await select(registry, operation) is first
        assert calls == [registry._catalog_path]
    await registry.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["authorize", "propose"])
async def test_changed_and_invalid_catalog_are_observed_on_next_route(registry, operation):
    async with registry.request_scope():
        old = await select(registry, operation)
        _change(registry)
        current = await select(registry, operation)
        assert current is not old
        assert current.settings.metadata == {"changed": True}
        assert not old.closed.is_set()
        good = registry._catalog_path.read_text()
        generation = registry._generation
        registry._catalog_path.write_text("councils: [")
        with pytest.raises(yaml.YAMLError):
            await select(registry, operation)
        assert registry._generation == generation
        assert registry._services["council-a"] is current
        registry._catalog_path.write_text(good)
        assert await select(registry, operation) is current
    await registry.aclose()
    assert old.closed.is_set() and current.closed.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["authorize", "propose"])
async def test_reload_during_initialization_selects_new_generation(registry, operation):
    FakeService.init_release = asyncio.Event()
    FakeService.init_entered = asyncio.Event()

    async def scoped_select():
        async with registry.request_scope():
            return await select(registry, operation)

    delayed = asyncio.create_task(scoped_select())
    await FakeService.init_entered.wait()
    old = FakeService.instances[0]
    _change(registry)
    current = await scoped_select()
    assert current is not old and current.settings.metadata == {"changed": True}
    assert not old.closed.is_set()
    FakeService.init_release.set()
    assert await delayed is current
    await asyncio.wait_for(old.closed.wait(), 1)
    assert registry._services["council-a"] is current
    await registry.aclose()
