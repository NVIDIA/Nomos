# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Runtime registry for routing requests to configured councils."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from nomos.config.settings import (
    CouncilCatalogSettings,
    _resolve_env_reference,
    load_council_catalog,
)
from nomos.llm.limiter import LimiterPool
from nomos.nat.functions import (
    API_VERSION_V2,
    AuthorizeInput,
    CouncilService,
    ProposeIntentInput,
)


def _catalog_fingerprint(path: Path) -> str:
    """Return a stable hash of the catalog and referenced profile files."""
    hasher = hashlib.sha256()
    raw_bytes = path.read_bytes()
    hasher.update(str(path.resolve()).encode())
    hasher.update(raw_bytes)

    try:
        raw = yaml.safe_load(raw_bytes)
    except Exception:
        return hasher.hexdigest()
    if not isinstance(raw, dict):
        return hasher.hexdigest()

    councils = raw.get("councils")
    if not isinstance(councils, dict):
        return hasher.hexdigest()

    base_dir = path.resolve().parent
    refs: list[Path] = []
    for entry in councils.values():
        if not isinstance(entry, dict):
            continue
        config_file = entry.get("config_file")
        if not isinstance(config_file, str) or not config_file:
            continue
        ref_path = Path(config_file)
        if not ref_path.is_absolute():
            ref_path = base_dir / ref_path
        refs.append(ref_path.resolve())

    for ref_path in sorted(set(refs)):
        hasher.update(str(ref_path).encode())
        if ref_path.exists():
            hasher.update(ref_path.read_bytes())
        else:
            hasher.update(b"<missing>")
    return hasher.hexdigest()


logger = logging.getLogger(__name__)


class _RequestScope:
    def __init__(self):
        self.active = True
        self.owner = asyncio.current_task()
        self.leases: set[tuple[CouncilRegistry, CouncilService]] = set()

    def acquire(self, registry: CouncilRegistry, service: CouncilService) -> None:
        if not self.active:
            raise RuntimeError("Council request scope has already closed")
        if self.owner is not asyncio.current_task():
            raise RuntimeError("Child tasks must enter their own council request scope")
        key = (registry, service)
        if key not in self.leases:
            self.leases.add(key)
            registry._lease_counts[service] = registry._lease_counts.get(service, 0) + 1
            registry._changed.clear()

    def release(self) -> None:
        self.active = False
        for registry, service in self.leases:
            registry._lease_counts[service] -= 1
            if not registry._lease_counts[service]:
                del registry._lease_counts[service]
                if service in registry._retired:
                    registry._schedule_close(service)
            registry._changed.set()
        self.leases.clear()


_request_scope: ContextVar[_RequestScope | None] = ContextVar("council_request_scope", default=None)


@asynccontextmanager
async def request_scope():
    """Scope service leases across a callback, including nested registry calls."""
    existing = _request_scope.get()
    if existing is not None and existing.active and existing.owner is asyncio.current_task():
        yield
        return
    scope = _RequestScope()
    token = _request_scope.set(scope)
    try:
        yield
    finally:
        scope.release()
        _request_scope.reset(token)


class CouncilRegistry:
    """Lazy service registry for one or more configured councils."""

    def __init__(self, catalog: CouncilCatalogSettings, catalog_path: Path | None = None):
        self._catalog = catalog
        self._catalog_path = catalog_path
        self._fingerprint = _catalog_fingerprint(catalog_path) if catalog_path else ""
        self._services: dict[str, CouncilService] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._reload_lock = asyncio.Lock()
        self._generation = 0
        self._closed = False
        self._owners = 0
        self._lease_counts: dict[CouncilService, int] = {}
        self._retired: set[CouncilService] = set()
        self._initializing: set[CouncilService] = set()
        self._close_tasks: dict[CouncilService, asyncio.Task[None]] = {}
        self._close_failed = False
        self._changed = asyncio.Event()
        self._changed.set()
        self._shutdown_task: asyncio.Task[None] | None = None
        # One pool per registry: every council in this process shares the
        # in-flight budget of each inference endpoint, and the pool survives
        # catalog reloads so rebuilt services keep the same limiters as
        # in-flight requests.
        self._limiter_pool = LimiterPool()

    def request_scope(self):
        """Hold selected services until the complete callback leaves this scope."""
        return request_scope()

    def retain_owner(self) -> None:
        if self._closed:
            raise RuntimeError("Council registry is closed")
        self._owners += 1

    async def release_owner(self) -> None:
        if self._owners <= 0:
            raise RuntimeError("Council registry owner released twice")
        self._owners -= 1
        if self._owners == 0:
            await self.aclose()

    def _retire(self, service: CouncilService) -> None:
        self._retired.add(service)
        if not self._lease_counts.get(service) and service not in self._initializing:
            self._schedule_close(service)

    def _schedule_close(self, service: CouncilService) -> None:
        if service in self._close_tasks:
            return

        async def close() -> None:
            try:
                await service.aclose()
            except Exception as exc:
                logger.error("Council service close failed: %s", type(exc).__name__)
                raise
            finally:
                self._retired.discard(service)
                self._changed.set()

        task = asyncio.create_task(close())
        self._close_tasks[service] = task

        def completed(done: asyncio.Task[None]) -> None:
            self._close_tasks.pop(service, None)
            if done.cancelled() or done.exception() is not None:
                self._close_failed = True
            self._changed.set()

        task.add_done_callback(completed)

    async def aclose(self) -> None:
        if self._shutdown_task is None:
            self._closed = True
            self._generation += 1
            for service in list(self._services.values()):
                self._retire(service)
            self._services.clear()
            self._shutdown_task = asyncio.create_task(self._drain())
        cancelled = False
        while True:
            try:
                await asyncio.shield(self._shutdown_task)
                break
            except asyncio.CancelledError:
                cancelled = True
                if self._shutdown_task.done():
                    self._shutdown_task.result()
                    break
        if cancelled:
            raise asyncio.CancelledError

    async def _drain(self) -> None:
        while self._lease_counts or self._initializing:
            self._changed.clear()
            await self._changed.wait()
        # Initialization/lease release schedules the final retired services.
        for service in list(self._retired):
            self._schedule_close(service)
        while self._close_tasks:
            await asyncio.gather(*tuple(self._close_tasks.values()), return_exceptions=True)
            # gather may already be done before its queued completion callbacks
            # remove entries; yield so the drain cannot spin on finished tasks.
            await asyncio.sleep(0)
        if self._close_failed:
            raise RuntimeError("One or more council services failed to close")

    @property
    def default_council_id(self) -> str:
        return self._catalog.default_council_id

    @property
    def council_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._catalog.councils))

    def resolve_council_id(self, requested: str = "", *, require_explicit: bool = False) -> str:
        """Resolve and validate a requested council id."""
        if require_explicit and not requested:
            raise ValueError("council_id is required for v2 council API calls")
        council_id = requested or self.default_council_id
        if council_id not in self._catalog.councils:
            raise ValueError(
                f"Unknown council_id {council_id!r}. Known councils: {list(self.council_ids)}"
            )
        if not self._catalog.get(council_id).enabled:
            raise ValueError(f"Council {council_id!r} is disabled")
        return council_id

    async def reload_if_changed(self) -> bool:
        """Reload the catalog when the backing file or referenced profiles changed."""
        if self._closed:
            raise RuntimeError("Council registry is closed")
        if self._catalog_path is None:
            return False
        async with self._reload_lock:
            if self._closed:
                raise RuntimeError("Council registry is closed")
            fingerprint = _catalog_fingerprint(self._catalog_path)
            if fingerprint == self._fingerprint:
                return False
            self._catalog = load_council_catalog(str(self._catalog_path))
            self._fingerprint = fingerprint
            # Future requests should use fresh CouncilService instances. In-flight
            # requests retain their existing object references.
            self._generation += 1
            for service in list(self._services.values()):
                self._retire(service)
            self._services.clear()
            self._locks.clear()
            return True

    def describe(self) -> dict[str, Any]:
        """Return lightweight registry metadata for operator tooling."""
        return {
            "default_council_id": self.default_council_id,
            "councils": [
                {
                    "council_id": council_id,
                    "enabled": settings.enabled,
                    "opa_policy_id": settings.opa.policy_id,
                    "members": [member.id for member in settings.members],
                    "ledger_path": settings.ledger_path,
                    "metadata": settings.metadata,
                    "loaded": council_id in self._services,
                }
                for council_id, settings in sorted(self._catalog.councils.items())
            ],
        }

    async def get(self, council_id: str = "") -> CouncilService:
        """Select a current service; request_scope supplies its operation lifetime."""
        await self.reload_if_changed()
        while True:
            if self._closed:
                raise RuntimeError("Council registry is closed")
            generation = self._generation
            selected = self.resolve_council_id(council_id)
            lock = self._locks.setdefault(selected, asyncio.Lock())
            async with lock:
                if generation != self._generation:
                    continue
                service = self._services.get(selected)
                if service is None:
                    service = CouncilService(
                        self._catalog.get(selected), limiter_pool=self._limiter_pool
                    )
                    self._initializing.add(service)
                    try:
                        await service.init_redis()
                    except BaseException:
                        self._retire(service)
                        raise
                    finally:
                        self._initializing.discard(service)
                        if service in self._retired:
                            self._schedule_close(service)
                        self._changed.set()
                    if self._closed or generation != self._generation:
                        self._retire(service)
                        continue
                    self._services[selected] = service
                scope = _request_scope.get()
                if scope is not None:
                    scope.acquire(self, service)
                return service

    async def service_for_propose(
        self,
        input_data: ProposeIntentInput,
        *,
        require_explicit: bool = False,
    ) -> tuple[CouncilService, ProposeIntentInput]:
        """Resolve a propose request to a service and normalized input."""
        await self.reload_if_changed()
        selected = self.resolve_council_id(
            input_data.council_id,
            require_explicit=require_explicit,
        )
        api_version = API_VERSION_V2 if require_explicit else input_data.api_version
        normalized = input_data.model_copy(
            update={"council_id": selected, "api_version": api_version}
        )
        return await self.get(selected), normalized

    async def service_for_authorize(
        self,
        input_data: AuthorizeInput,
        *,
        require_explicit: bool = False,
    ) -> tuple[CouncilService, AuthorizeInput]:
        """Resolve an authorize request to a service and normalized input."""
        await self.reload_if_changed()
        selected = self.resolve_council_id(
            input_data.council_id,
            require_explicit=require_explicit,
        )
        api_version = API_VERSION_V2 if require_explicit else input_data.api_version
        normalized = input_data.model_copy(
            update={"council_id": selected, "api_version": api_version}
        )
        return await self.get(selected), normalized


_registries: dict[Path, CouncilRegistry] = {}
_registry_lock: asyncio.Lock | None = None


def _get_registry_lock() -> asyncio.Lock:
    global _registry_lock
    if _registry_lock is None:
        _registry_lock = asyncio.Lock()
    return _registry_lock


async def get_registry(config_file: str) -> CouncilRegistry:
    """Load and cache a registry for a council config path."""
    config_file = str(_resolve_env_reference(config_file, default=config_file))
    path = Path(config_file).resolve()
    async with _get_registry_lock():
        if path not in _registries or _registries[path]._closed:
            _registries[path] = CouncilRegistry(load_council_catalog(str(path)), path)
        return _registries[path]
