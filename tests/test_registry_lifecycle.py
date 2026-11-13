"""Registry lifecycle through the actual registered NAT callback surface."""

import asyncio
import inspect
import json
import threading
from types import SimpleNamespace
from typing import ClassVar

import pytest
import yaml

from nomos.config.settings import load_council_catalog
from nomos.nat import plugin
from nomos.nat.functions import AuthorizeInput, LedgerPageInput, ProposeIntentInput
from nomos.nat.registry import CouncilRegistry
from tests.test_council_registry import _write_catalog


class FakeService:
    instances: ClassVar[list] = []
    init_release = None
    init_entered = None

    def __init__(self, settings, limiter_pool=None):
        self.settings = settings
        self.closed = asyncio.Event()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.active = False
        self.initialized = False
        self.read_entered = threading.Event()
        self.read_release = threading.Event()
        type(self).instances.append(self)

    async def init_redis(self):
        if len(type(self).instances) == 1 and type(self).init_release is not None:
            type(self).init_entered.set()
            await type(self).init_release.wait()
        self.initialized = True

    async def aclose(self):
        assert self.initialized and not self.active
        self.closed.set()

    async def authorize(self, input_data, **kwargs):
        return await self._review()

    async def propose_intent(self, input_data, **kwargs):
        return await self._review()

    async def _review(self):
        self.active = True
        self.entered.set()
        try:
            await self.release.wait()
            assert not self.closed.is_set()
            return SimpleNamespace(model_dump_json=lambda: '{"done":true}')
        finally:
            self.active = False

    def get_ledger_page(self, input_data):
        self.active = True
        self.read_entered.set()
        try:
            self.read_release.wait(3)
            assert not self.closed.is_set()
            return SimpleNamespace(model_dump_json=lambda: '{"entries":[]}')
        finally:
            self.active = False


@pytest.fixture
def registry(tmp_path, monkeypatch):
    FakeService.instances = []
    FakeService.init_release = FakeService.init_entered = None
    monkeypatch.setattr("nomos.nat.registry.CouncilService", FakeService)
    path = _write_catalog(tmp_path)
    registry = CouncilRegistry(load_council_catalog(str(path)), path)

    async def get_registry(_):
        return registry

    monkeypatch.setattr(plugin, "_get_registry", get_registry)
    return registry


def _change(registry):
    path = registry._catalog_path
    data = yaml.safe_load(path.read_text())
    data["shared"]["metadata"] = {"changed": True}
    path.write_text(yaml.safe_dump(data))


def _request(authorize):
    common = dict(
        agent_id="agent",
        council_id="council-a",
        description="Read workspace for test",
        target_resource="/workspace",
        justification="lifecycle test",
    )
    return (
        AuthorizeInput(**common, command="pwd", working_directory="/workspace")
        if authorize
        else ProposeIntentInput(**common)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,config,authorize",
    [
        ("council_authorize", plugin.AuthorizeConfig, True),
        ("council_v1_authorize", plugin.AuthorizeV1Config, True),
        ("council_v2_authorize", plugin.AuthorizeV2Config, True),
        ("council_propose_intent", plugin.ProposeIntentConfig, False),
        ("council_v1_propose_intent", plugin.ProposeIntentV1Config, False),
        ("council_v2_propose_intent", plugin.ProposeIntentV2Config, False),
    ],
)
async def test_registered_alias_holds_old_service_across_reload(registry, name, config, authorize):
    factory = getattr(plugin, name)
    assert inspect.isasyncgenfunction(factory.__wrapped__)
    async with factory(config(council_config_file="ignored"), None) as callback:
        assert list(inspect.signature(callback).parameters) == ["input_data"]
        request = asyncio.create_task(callback(_request(authorize)))
        while not FakeService.instances:
            await asyncio.sleep(0)
        old = FakeService.instances[0]
        try:
            await asyncio.wait_for(old.entered.wait(), 2)
        except TimeoutError:
            if request.done():
                request.result()
            request.cancel()
            raise
        assert registry._lease_counts[old] == 1
        _change(registry)
        assert await registry.reload_if_changed()
        new = await registry.get("council-a")
        assert new is not old and not old.closed.is_set()
        old.release.set()
        assert json.loads(await request) == {"done": True}
        await asyncio.wait_for(old.closed.wait(), 1)
    assert new.closed.is_set()


@pytest.mark.asyncio
async def test_multiple_tool_owners_drain_callback_before_shutdown(registry):
    first = plugin.council_authorize(plugin.AuthorizeConfig(), None)
    second = plugin.council_v1_authorize(plugin.AuthorizeV1Config(), None)
    callback = await first.__aenter__()
    await second.__aenter__()
    request = asyncio.create_task(callback(_request(True)))
    while not FakeService.instances:
        await asyncio.sleep(0)
    service = FakeService.instances[0]
    await service.entered.wait()
    await first.__aexit__(None, None, None)
    assert not registry._closed
    close = asyncio.create_task(second.__aexit__(None, None, None))
    await asyncio.sleep(0)
    close.cancel()
    await asyncio.sleep(0)
    close.cancel()
    assert not service.closed.is_set()
    service.release.set()
    await request
    with pytest.raises(asyncio.CancelledError):
        await close
    assert service.closed.is_set()


@pytest.mark.asyncio
async def test_cancelled_registered_read_keeps_lease_until_thread_returns(registry):
    context = plugin.council_ledger_page(plugin.LedgerPageConfig(), None)
    callback = await context.__aenter__()
    request = asyncio.create_task(callback(LedgerPageInput(council_id="council-a")))
    while not FakeService.instances:
        await asyncio.sleep(0)
    service = FakeService.instances[0]
    while not service.read_entered.is_set():
        await asyncio.sleep(0.001)
    request.cancel()
    await asyncio.sleep(0)
    request.cancel()
    close = asyncio.create_task(context.__aexit__(None, None, None))
    await asyncio.sleep(0.01)
    assert not request.done() and not service.closed.is_set()
    service.read_release.set()
    with pytest.raises(asyncio.CancelledError):
        await request
    await close
    assert service.closed.is_set()


@pytest.mark.asyncio
async def test_delayed_initialization_cannot_install_old_generation(registry):
    FakeService.init_release = asyncio.Event()
    FakeService.init_entered = asyncio.Event()

    async def select():
        async with registry.request_scope():
            return await registry.get("council-a")

    first = asyncio.create_task(select())
    await FakeService.init_entered.wait()
    old = FakeService.instances[0]
    _change(registry)
    assert await registry.reload_if_changed()
    new = await select()
    assert new is not old
    assert not old.closed.is_set()
    FakeService.init_release.set()
    assert await first is new
    await asyncio.wait_for(old.closed.wait(), 1)
    assert registry._services["council-a"] is new
    await registry.aclose()


@pytest.mark.asyncio
async def test_nested_and_inherited_closed_scopes_do_not_lose_leases(registry):
    late = asyncio.Event()

    async def child():
        await late.wait()
        with pytest.raises(RuntimeError, match="scope has already closed"):
            await registry.get("council-a")
        async with registry.request_scope():
            return await registry.get("council-a")

    async with registry.request_scope():
        service = await registry.get("council-a")
        async with registry.request_scope():
            assert await registry.get("council-a") is service
            assert registry._lease_counts[service] == 1
        task = asyncio.create_task(child())
    assert not registry._lease_counts
    late.set()
    assert await task is service
    assert not registry._lease_counts
    await registry.aclose()


def test_every_registered_tool_preserves_generator_wrapping():
    names = [
        name
        for name, value in vars(plugin).items()
        if name.startswith("council_") and callable(value) and hasattr(value, "__wrapped__")
    ]
    assert len(names) == 18
    for name in names:
        factory = getattr(plugin, name)
        assert inspect.isasyncgenfunction(factory.__wrapped__)
        assert inspect.isasyncgenfunction(factory.__wrapped__.__wrapped__)
        assert inspect.signature(factory) == inspect.signature(factory.__wrapped__.__wrapped__)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,config_name,input_name,method,asynchronous",
    [
        (
            "council_check_status",
            "CheckStatusConfig",
            "IntentStatusInput",
            "check_intent_status_async",
            True,
        ),
        (
            "council_pending_approvals",
            "PendingApprovalsConfig",
            "PendingApprovalsInput",
            "get_pending_approvals_async",
            True,
        ),
        ("council_get_public_key", "PublicKeyConfig", "PublicKeyInput", "get_public_key", False),
        ("council_ledger_get", "LedgerEntryConfig", "LedgerEntryInput", "get_ledger_entry", False),
        (
            "council_check_writ_status",
            "WritStatusConfig",
            "WritStatusInput",
            "check_writ_status",
            True,
        ),
    ],
)
async def test_registered_read_status_paths_hold_request_lease(
    registry, monkeypatch, name, config_name, input_name, method, asynchronous
):
    from nomos.nat import functions

    def result(service, *args):
        assert registry._lease_counts[service] == 1
        return SimpleNamespace(model_dump_json=lambda: '{"leased":true}')

    async def async_result(service, *args):
        await asyncio.sleep(0)
        return result(service, *args)

    monkeypatch.setattr(
        FakeService, method, async_result if asynchronous else result, raising=False
    )
    request = getattr(functions, input_name).model_construct(
        council_id="council-a", intent_id="intent-test", writ_id="writ-test", sequence=0
    )
    factory = getattr(plugin, name)
    async with factory(getattr(plugin, config_name)(), None) as callback:
        assert json.loads(await callback(request)) == {"leased": True}
    assert FakeService.instances[0].closed.is_set()


@pytest.mark.asyncio
async def test_registered_management_callback_has_scope_and_owner(registry, monkeypatch):
    from nomos.nat.functions import CouncilListInput
    from nomos.nat.registry import _request_scope

    observed = []

    def catalog():
        observed.append(_request_scope.get().active)
        return registry._catalog

    monkeypatch.setattr(
        plugin,
        "_store_for_config",
        lambda _: SimpleNamespace(load_catalog=catalog, list_councils=lambda: []),
    )

    async def forbidden(_):
        raise AssertionError("management eagerly loaded runtime registry")

    monkeypatch.setattr(plugin, "_get_registry", forbidden)
    async with plugin.council_list_councils(plugin.CouncilListConfig(), None) as callback:
        await callback(CouncilListInput())
        assert observed == [True] and registry._owners == 0
    await registry.aclose()


@pytest.mark.asyncio
async def test_child_scope_owns_lease_after_parent_scope_exits(registry):
    ready = asyncio.Event()
    release = asyncio.Event()

    async def child():
        with pytest.raises(RuntimeError, match="Child tasks"):
            await registry.get("council-a")
        async with registry.request_scope():
            selected = await registry.get("council-a")
            ready.set()
            await release.wait()
            assert not selected.closed.is_set()

    async with registry.request_scope():
        service = await registry.get("council-a")
        task = asyncio.create_task(child())
        await ready.wait()
        assert registry._lease_counts[service] == 2
    assert registry._lease_counts[service] == 1
    closing = asyncio.create_task(registry.aclose())
    await asyncio.sleep(0)
    assert not service.closed.is_set()
    release.set()
    await task
    await closing
    assert service.closed.is_set()


@pytest.mark.asyncio
async def test_shutdown_waits_for_initialization_and_disposes_result(registry):
    FakeService.init_release = asyncio.Event()
    FakeService.init_entered = asyncio.Event()
    selection = asyncio.create_task(registry.get("council-a"))
    await FakeService.init_entered.wait()
    service = FakeService.instances[0]
    closing = asyncio.create_task(registry.aclose())
    await asyncio.sleep(0)
    assert not closing.done() and not service.closed.is_set()
    FakeService.init_release.set()
    with pytest.raises(RuntimeError, match="closed"):
        await selection
    await closing
    assert service.closed.is_set() and not registry._services


@pytest.mark.asyncio
async def test_failed_initialization_disposes_partial_service(registry, monkeypatch):
    closed = asyncio.Event()

    async def fail(self):
        raise ValueError("initialization failed")

    async def close(self):
        closed.set()

    monkeypatch.setattr(FakeService, "init_redis", fail)
    monkeypatch.setattr(FakeService, "aclose", close)
    with pytest.raises(ValueError, match="initialization failed"):
        await registry.get("council-a")
    await asyncio.wait_for(closed.wait(), 1)
    await registry.aclose()
    assert not registry._services and not registry._initializing


@pytest.mark.asyncio
async def test_failed_service_close_remains_owned_and_can_retry(registry, monkeypatch):
    failures = []

    async def close(self):
        if not failures:
            failures.append(True)
            raise RuntimeError("injected cleanup failure")
        self.closed.set()

    monkeypatch.setattr(FakeService, "aclose", close)
    service = await registry.get("council-a")
    with pytest.raises(RuntimeError, match="failed to close"):
        await registry.aclose()
    assert service in registry._retired and not service.closed.is_set()
    await registry.aclose()
    assert service.closed.is_set() and service not in registry._retired
