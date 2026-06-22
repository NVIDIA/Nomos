# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


"""Runtime registry for routing requests to configured councils."""

from __future__ import annotations

import asyncio
import hashlib
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


class CouncilRegistry:
    """Lazy service registry for one or more configured councils."""

    def __init__(self, catalog: CouncilCatalogSettings, catalog_path: Path | None = None):
        self._catalog = catalog
        self._catalog_path = catalog_path
        self._fingerprint = _catalog_fingerprint(catalog_path) if catalog_path else ""
        self._services: dict[str, CouncilService] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._reload_lock = asyncio.Lock()
        # One pool per registry: every council in this process shares the
        # in-flight budget of each inference endpoint, and the pool survives
        # catalog reloads so rebuilt services keep the same limiters as
        # in-flight requests.
        self._limiter_pool = LimiterPool()

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
        if self._catalog_path is None:
            return False
        async with self._reload_lock:
            fingerprint = _catalog_fingerprint(self._catalog_path)
            if fingerprint == self._fingerprint:
                return False
            self._catalog = load_council_catalog(str(self._catalog_path))
            self._fingerprint = fingerprint
            # Future requests should use fresh CouncilService instances. In-flight
            # requests retain their existing object references.
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
        """Return the initialized service for a council id."""
        await self.reload_if_changed()
        selected = self.resolve_council_id(council_id)
        lock = self._locks.setdefault(selected, asyncio.Lock())
        async with lock:
            if selected not in self._services:
                service = CouncilService(
                    self._catalog.get(selected),
                    limiter_pool=self._limiter_pool,
                )
                await service.init_redis()
                self._services[selected] = service
            return self._services[selected]

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
        if path not in _registries:
            _registries[path] = CouncilRegistry(load_council_catalog(str(path)), path)
        return _registries[path]
