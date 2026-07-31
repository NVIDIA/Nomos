# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import json

import pytest
import yaml

from nomos.config import council_catalog_store as catalog_store
from nomos.config.council_catalog_store import CouncilCatalogStore, CouncilCatalogStoreError
from nomos.config.settings import load_council_catalog
from nomos.nat.functions import CouncilCreateInput, CouncilListInput, ProposeIntentInput
from nomos.nat.registry import CouncilRegistry


def _minimal_profile(council_id: str = "template-council") -> dict:
    return {
        "council_id": council_id,
        "require_auth": False,
        "require_council_membership": True,
        "llm_backends": {},
        "members": [],
        "opa": {"enabled": False},
        "council_key_path": "",
        "ledger_path": "",
        "redis": {"host": ""},
    }


def _write_store(tmp_path, monkeypatch) -> CouncilCatalogStore:
    catalog_path = tmp_path / "catalog.yaml"
    template_path = tmp_path / "template.yaml"
    template_path.write_text(yaml.safe_dump(_minimal_profile("council-a")))
    catalog_path.write_text(
        yaml.safe_dump(
            {
                "default_council_id": "council-a",
                "councils": {"council-a": {"config_file": "template.yaml"}},
            }
        )
    )
    monkeypatch.setitem(catalog_store.TEMPLATE_CONFIGS, "test-template", str(template_path))
    monkeypatch.setenv("COUNCIL_DYNAMIC_LEDGER_DIR", str(tmp_path / "ledger"))
    return CouncilCatalogStore(catalog_path=catalog_path, profile_dir=tmp_path / "councils")


def test_catalog_store_creates_disabled_council_from_template(tmp_path, monkeypatch):
    store = _write_store(tmp_path, monkeypatch)

    settings = store.create_council(
        "council-b",
        template="test-template",
        description="Customer workflow council",
        use_case="Review generated traces",
    )

    assert settings.council_id == "council-b"
    assert settings.enabled is False
    assert settings.require_auth is True
    assert settings.require_council_membership is True
    assert settings.ledger_path == str(tmp_path / "ledger" / "council-b-ledger.jsonl")
    assert settings.metadata["template"] == "test-template"

    catalog = load_council_catalog(str(store.catalog_path))
    assert sorted(catalog.councils) == ["council-a", "council-b"]
    assert catalog.get("council-b").enabled is False


def test_catalog_store_rejects_unsafe_council_ids(tmp_path, monkeypatch):
    store = _write_store(tmp_path, monkeypatch)

    with pytest.raises(CouncilCatalogStoreError, match="council_id"):
        store.create_council("../prod", template="test-template")


def test_catalog_store_updates_council_from_profile_file(tmp_path, monkeypatch):
    store = _write_store(tmp_path, monkeypatch)
    profile_path = tmp_path / "replacement.yaml"
    replacement = _minimal_profile("ignored-id")
    replacement["enabled"] = True
    replacement["metadata"] = {"owner": "alpha"}
    profile_path.write_text(yaml.safe_dump(replacement))

    settings = store.update_council_from_file("customer-git", profile_path)

    assert settings.council_id == "customer-git"
    assert settings.enabled is True
    assert settings.metadata["owner"] == "alpha"
    assert settings.metadata["source_file"] == str(profile_path)
    catalog = load_council_catalog(str(store.catalog_path))
    assert catalog.get("customer-git").council_id == "customer-git"


def test_catalog_store_updates_council_from_profile_mapping(tmp_path, monkeypatch):
    store = _write_store(tmp_path, monkeypatch)
    replacement = _minimal_profile("ignored-id")
    replacement["enabled"] = True
    replacement["metadata"] = {"owner": "alpha"}

    settings = store.update_council_profile("customer-git", replacement, enabled=False)

    assert settings.council_id == "customer-git"
    assert settings.enabled is False
    assert settings.metadata["owner"] == "alpha"
    assert settings.metadata["source"] == "mcp"
    catalog = load_council_catalog(str(store.catalog_path))
    assert catalog.get("customer-git").enabled is False


async def _call_registered_tool(registration, config, input_data):
    async with registration(config, None) as tool:
        return json.loads(await tool(input_data))


@pytest.mark.asyncio
async def test_dynamic_council_mcp_management_tools_list_and_create(tmp_path, monkeypatch):
    pytest.importorskip("nat")
    from nomos.nat import plugin

    store = _write_store(tmp_path, monkeypatch)

    listing = await _call_registered_tool(
        plugin.council_list_councils,
        plugin.CouncilListConfig(council_config_file=str(store.catalog_path)),
        CouncilListInput(),
    )

    assert listing["success"] is True
    assert listing["default_council_id"] == "council-a"
    assert [c["council_id"] for c in listing["councils"]] == ["council-a"]

    blocked = await _call_registered_tool(
        plugin.council_create_council,
        plugin.CouncilCreateConfig(council_config_file=str(store.catalog_path)),
        CouncilCreateInput(
            council_id="customer-git",
            management_token="wrong",
            template="test-template",
        ),
    )

    assert blocked["success"] is False
    assert "not configured" in blocked["error"]

    monkeypatch.setenv("COUNCIL_MANAGEMENT_TOKEN", "secret-token")
    bad_token = await _call_registered_tool(
        plugin.council_create_council,
        plugin.CouncilCreateConfig(council_config_file=str(store.catalog_path)),
        CouncilCreateInput(
            council_id="customer-git",
            management_token="wrong",
            template="test-template",
        ),
    )

    assert bad_token["success"] is False
    assert "Invalid or missing" in bad_token["error"]

    created = await _call_registered_tool(
        plugin.council_create_council,
        plugin.CouncilCreateConfig(council_config_file=str(store.catalog_path)),
        CouncilCreateInput(
            council_id="customer-git",
            management_token="secret-token",
            template="test-template",
            description="Customer git workflow council",
            enabled=True,
        ),
    )

    assert created["success"] is True
    assert created["council_id"] == "customer-git"
    assert created["council"]["enabled"] is True


@pytest.mark.asyncio
async def test_registry_hot_reloads_new_enabled_council(tmp_path, monkeypatch):
    store = _write_store(tmp_path, monkeypatch)
    registry = CouncilRegistry(load_council_catalog(str(store.catalog_path)), store.catalog_path)

    assert registry.council_ids == ("council-a",)

    store.create_council("council-b", template="test-template", enabled=True)

    service, normalized = await registry.service_for_propose(
        ProposeIntentInput(
            agent_id="agent-1",
            council_id="council-b",
            description="Read workspace state before continuing",
            target_resource="/workspace",
            justification="Need to inspect current files",
        ),
        require_explicit=True,
    )

    assert service.council_id == "council-b"
    assert normalized.council_id == "council-b"
    assert registry.council_ids == ("council-a", "council-b")


@pytest.mark.asyncio
async def test_registry_rejects_disabled_council(tmp_path, monkeypatch):
    store = _write_store(tmp_path, monkeypatch)
    store.create_council("council-b", template="test-template", enabled=False)
    registry = CouncilRegistry(load_council_catalog(str(store.catalog_path)), store.catalog_path)

    with pytest.raises(ValueError, match="disabled"):
        await registry.service_for_propose(
            ProposeIntentInput(
                agent_id="agent-1",
                council_id="council-b",
                description="Read workspace state before continuing",
                target_resource="/workspace",
                justification="Need to inspect current files",
            ),
            require_explicit=True,
        )

    store.set_enabled("council-b", True)
    service, normalized = await registry.service_for_propose(
        ProposeIntentInput(
            agent_id="agent-1",
            council_id="council-b",
            description="Read workspace state before continuing",
            target_resource="/workspace",
            justification="Need to inspect current files",
        ),
        require_explicit=True,
    )
    assert service.council_id == "council-b"
    assert normalized.council_id == "council-b"


def test_management_mutations_disabled_in_postgres_mode(monkeypatch):
    pytest.importorskip("nat", reason="nvidia-nat not installed")
    from nomos.nat.plugin import _management_token_error

    monkeypatch.setenv("COUNCIL_STORAGE_MODE", "postgres")
    error = _management_token_error(object(), "any-token", mutating=True)
    assert "disabled in postgres storage mode" in error
    # reads stay available
    assert _management_token_error(object(), "", mutating=False) == ""

    monkeypatch.setenv("COUNCIL_STORAGE_MODE", "redis")
    assert _management_token_error(object(), "", mutating=True) == ""
