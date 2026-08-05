# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""File-backed management helpers for dynamic council catalogs."""

from __future__ import annotations

import copy
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from nomos.config.settings import (
    CouncilCatalogSettings,
    CouncilSettings,
    load_council_catalog,
)

COUNCIL_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
DEFAULT_CATALOG_PATH = "configs/local_council_catalog.yaml"
DEFAULT_DYNAMIC_LEDGER_DIR = "/app/ledger"

TEMPLATE_CONFIGS: dict[str, str] = {
    "strict-production": "configs/council.yaml",
    "default": "configs/council.yaml",
    "git-workflow-local": "configs/git_workflow_council.yaml",
}


class CouncilCatalogStoreError(Exception):
    """Expected operator-facing catalog management failure."""


@dataclass(frozen=True)
class CouncilCatalogStore:
    """Manage a writable council catalog and generated profile files."""

    catalog_path: Path
    profile_dir: Path

    @classmethod
    def from_env(cls) -> CouncilCatalogStore:
        catalog_path = Path(
            os.environ.get("COUNCIL_CATALOG_PATH")
            or os.environ.get("NOMOS_COUNCIL_CATALOG_PATH")
            or DEFAULT_CATALOG_PATH
        )
        profile_dir = Path(
            os.environ.get("COUNCIL_DYNAMIC_PROFILE_DIR")
            or os.environ.get("NOMOS_COUNCIL_PROFILE_DIR")
            or str(catalog_path.parent / "councils")
        )
        return cls(catalog_path=catalog_path, profile_dir=profile_dir)

    @property
    def catalog_dir(self) -> Path:
        return self.catalog_path.parent

    def validate_council_id(self, council_id: str) -> str:
        if not COUNCIL_ID_RE.fullmatch(council_id):
            raise CouncilCatalogStoreError(
                "council_id must contain only letters, numbers, hyphen, underscore, or dot"
            )
        return council_id

    def profile_path(self, council_id: str) -> Path:
        council_id = self.validate_council_id(council_id)
        path = (self.profile_dir / f"{council_id}.yaml").resolve()
        profile_root = self.profile_dir.resolve()
        if profile_root not in path.parents:
            raise CouncilCatalogStoreError("resolved profile path escaped profile directory")
        return path

    def load_catalog(self) -> CouncilCatalogSettings:
        return load_council_catalog(str(self.catalog_path))

    def load_raw_catalog(self) -> dict[str, Any]:
        if not self.catalog_path.exists():
            raise CouncilCatalogStoreError(f"catalog file not found: {self.catalog_path}")
        raw = yaml.safe_load(self.catalog_path.read_text())
        if not isinstance(raw, dict):
            raise CouncilCatalogStoreError("catalog file must contain a YAML mapping")
        if "councils" not in raw or not isinstance(raw["councils"], dict):
            raise CouncilCatalogStoreError("catalog file must contain a councils mapping")
        return raw

    def _read_template(self, template: str) -> dict[str, Any]:
        try:
            template_path = TEMPLATE_CONFIGS[template]
        except KeyError as exc:
            known = ", ".join(sorted(TEMPLATE_CONFIGS))
            raise CouncilCatalogStoreError(
                f"unknown template {template!r}; known: {known}"
            ) from exc

        path = Path(template_path)
        if not path.is_absolute():
            candidate = self.catalog_dir / path
            path = candidate if candidate.exists() else Path.cwd() / path
        if not path.exists():
            raise CouncilCatalogStoreError(f"template config not found: {template_path}")

        raw = yaml.safe_load(path.read_text())
        if not isinstance(raw, dict):
            raise CouncilCatalogStoreError(f"template config {path} must contain a mapping")
        return raw

    def _relative_profile_ref(self, path: Path) -> str:
        try:
            return path.relative_to(self.catalog_dir.resolve()).as_posix()
        except ValueError:
            return str(path)

    def _atomic_write_yaml(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = yaml.safe_dump(data, sort_keys=False)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(path.parent),
            delete=False,
        ) as tmp:
            tmp.write(encoded)
            tmp_path = Path(tmp.name)
        tmp_path.replace(path)

    def _dynamic_ledger_path(self, council_id: str) -> str:
        ledger_dir = os.environ.get("COUNCIL_DYNAMIC_LEDGER_DIR", DEFAULT_DYNAMIC_LEDGER_DIR)
        if not ledger_dir:
            return ""
        return str(Path(ledger_dir) / f"{council_id}-ledger.jsonl")

    def create_council(
        self,
        council_id: str,
        *,
        template: str = "strict-production",
        description: str = "",
        use_case: str = "",
        enabled: bool = False,
        force: bool = False,
    ) -> CouncilSettings:
        """Create a profile file and add it to the catalog."""
        council_id = self.validate_council_id(council_id)
        raw_catalog = self.load_raw_catalog()
        if council_id in raw_catalog["councils"] and not force:
            raise CouncilCatalogStoreError(f"council {council_id!r} already exists")

        profile = copy.deepcopy(self._read_template(template))
        profile["council_id"] = council_id
        profile["enabled"] = enabled
        profile["require_auth"] = True
        profile["require_council_membership"] = True
        profile["ledger_path"] = self._dynamic_ledger_path(council_id)
        metadata = dict(profile.get("metadata") or {})
        metadata.update(
            {
                "created_at": str(time.time()),
                "template": template,
                "description": description,
                "use_case": use_case,
            }
        )
        profile["metadata"] = metadata

        # Validate before writing anything visible in the catalog.
        settings = CouncilSettings.model_validate(profile)
        profile_path = self.profile_path(council_id)
        self._atomic_write_yaml(profile_path, profile)

        raw_catalog["councils"][council_id] = {
            "config_file": self._relative_profile_ref(profile_path)
        }
        self._atomic_write_yaml(self.catalog_path, raw_catalog)
        return settings

    def update_council_from_file(
        self,
        council_id: str,
        source_path: Path,
        *,
        enabled: bool | None = None,
    ) -> CouncilSettings:
        """Replace a council profile with a validated YAML profile file."""
        council_id = self.validate_council_id(council_id)
        raw_catalog = self.load_raw_catalog()
        if not source_path.exists():
            raise CouncilCatalogStoreError(f"profile file not found: {source_path}")

        profile = yaml.safe_load(source_path.read_text())
        if not isinstance(profile, dict):
            raise CouncilCatalogStoreError(f"profile {source_path} must contain a mapping")
        return self.update_council_profile(
            council_id,
            profile,
            enabled=enabled,
            metadata_updates={"source": "file", "source_file": str(source_path)},
            raw_catalog=raw_catalog,
        )

    def update_council_profile(
        self,
        council_id: str,
        profile: dict[str, Any],
        *,
        enabled: bool | None = None,
        metadata_updates: dict[str, Any] | None = None,
        raw_catalog: dict[str, Any] | None = None,
    ) -> CouncilSettings:
        """Replace a council profile with a validated profile mapping."""
        council_id = self.validate_council_id(council_id)
        raw_catalog = raw_catalog or self.load_raw_catalog()
        profile = copy.deepcopy(profile)
        profile["council_id"] = council_id
        if enabled is not None:
            profile["enabled"] = enabled
        profile.setdefault("require_auth", True)
        profile.setdefault("require_council_membership", True)
        metadata = dict(profile.get("metadata") or {})
        metadata.update({"updated_at": str(time.time()), "source": "mcp"})
        if metadata_updates:
            metadata.update(metadata_updates)
        profile["metadata"] = metadata

        settings = CouncilSettings.model_validate(profile)
        profile_path = self.profile_path(council_id)
        self._atomic_write_yaml(profile_path, profile)
        raw_catalog["councils"][council_id] = {
            "config_file": self._relative_profile_ref(profile_path)
        }
        self._atomic_write_yaml(self.catalog_path, raw_catalog)
        return settings

    def set_enabled(self, council_id: str, enabled: bool) -> CouncilSettings:
        council_id = self.validate_council_id(council_id)
        raw_catalog = self.load_raw_catalog()
        try:
            entry = raw_catalog["councils"][council_id]
        except KeyError as exc:
            raise CouncilCatalogStoreError(f"unknown council {council_id!r}") from exc
        if not isinstance(entry, dict) or not entry.get("config_file"):
            raise CouncilCatalogStoreError(
                f"council {council_id!r} is not file-backed and cannot be toggled"
            )

        profile_ref = Path(str(entry["config_file"]))
        if not profile_ref.is_absolute():
            profile_ref = self.catalog_dir / profile_ref
        profile = yaml.safe_load(profile_ref.read_text())
        if not isinstance(profile, dict):
            raise CouncilCatalogStoreError(f"profile {profile_ref} must contain a mapping")
        profile["enabled"] = enabled
        settings = CouncilSettings.model_validate(profile)
        self._atomic_write_yaml(profile_ref, profile)
        return settings

    def validate(self) -> CouncilCatalogSettings:
        """Validate the catalog and all referenced council profiles."""
        return self.load_catalog()

    def list_councils(self) -> list[dict[str, Any]]:
        catalog = self.load_catalog()
        return [
            {
                "council_id": council_id,
                "default": council_id == catalog.default_council_id,
                "enabled": settings.enabled,
                "opa_policy_id": settings.opa.policy_id,
                "members": [member.id for member in settings.members],
                "ledger_path": settings.ledger_path,
                "metadata": settings.metadata,
            }
            for council_id, settings in sorted(catalog.councils.items())
        ]

    def show_council(self, council_id: str) -> dict[str, Any]:
        council_id = self.validate_council_id(council_id)
        catalog = self.load_catalog()
        try:
            settings = catalog.get(council_id)
        except KeyError as exc:
            raise CouncilCatalogStoreError(f"unknown council {council_id!r}") from exc
        return {
            "council_id": council_id,
            "default": council_id == catalog.default_council_id,
            "enabled": settings.enabled,
            "opa_policy_id": settings.opa.policy_id,
            "members": [member.model_dump() for member in settings.members],
            "chairman": settings.chairman.model_dump(),
            "code_review": settings.code_review.model_dump(),
            "ledger_path": settings.ledger_path,
            "metadata": settings.metadata,
        }
