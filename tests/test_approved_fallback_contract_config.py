# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Independent F01-F04 config contracts, written from the accepted plan and baseline."""

import copy
import json

import pytest
from pydantic import ValidationError

from nomos.config.settings import (
    CouncilSettings,
    load_config,
    load_council_catalog,
    validate_council_deployment_preflight,
)


def config_data():
    return {
        "llm_max_retries": 2,
        "llm_retry_budget_seconds": 1.0,
        "llm_backends": {
            name: {
                "provider": "openai",
                "model": name + "-model",
                "api_key": "offline-test-key",  # pragma: allowlist secret - inert offline fixture
                "sdk_max_retries": 0,
                "request_timeout_seconds": 0.2,
            }
            for name in ("primary", "backup", "second", "unused")
        },
        "llm_fallbacks": {"primary": ["backup", "second"]},
        "members": [{"id": "m", "backend": "primary", "priorities": ["Review safely"]}],
        "chairman": {"enabled": False},
        "code_review": {"enabled": False},
    }


def test_default_fallbacks_are_empty_without_changing_legacy_sdk_options():
    cfg = CouncilSettings(
        llm_backends={"unused": {"provider": "openai", "model": "old", "sdk_max_retries": 3}}
    )
    assert cfg.model_dump().get("llm_fallbacks") == {}, "F01 absent routes must dump as {}"
    assert cfg.llm_backends["unused"].sdk_max_retries == 3, "F01 unconfigured SDK retries=3"


@pytest.mark.parametrize("backups", [["backup"], ["second", "backup"]])
def test_route_order_survives_direct_single_and_catalog_loaders(tmp_path, backups):
    data = config_data()
    data["llm_fallbacks"] = {"primary": backups}
    cfg = CouncilSettings.model_validate(data)
    assert getattr(cfg, "llm_fallbacks", None) == {"primary": backups}, f"F02 {backups=}"
    path = tmp_path / "qa-single.json"
    path.write_text(json.dumps(data))
    assert load_config(str(path)).llm_fallbacks["primary"] == backups, f"F02 single {backups=}"
    assert load_council_catalog(str(path)).get().llm_fallbacks["primary"] == backups


def test_catalog_arrays_replace_and_targets_cannot_resolve_in_other_council(tmp_path):
    data = config_data()
    path = tmp_path / "qa-catalog.json"
    catalog = {
        "shared": data,
        "councils": {"a": {}, "b": {"llm_fallbacks": {"primary": ["second"]}}},
    }
    path.write_text(json.dumps(catalog))
    cfg = load_council_catalog(str(path))
    assert getattr(cfg.get("a"), "llm_fallbacks", None) == {"primary": ["backup", "second"]}
    assert cfg.get("b").llm_fallbacks == {"primary": ["second"]}, "F02 arrays replace"
    isolated = {
        "councils": {
            "a": {**data, "llm_fallbacks": {"primary": ["foreign"]}},
            "b": {"llm_backends": {"foreign": data["llm_backends"]["backup"]}},
        }
    }
    path.write_text(json.dumps(isolated))
    with pytest.raises((ValidationError, ValueError), match=r"(?i)foreign|fallback|backend"):
        load_council_catalog(str(path))


@pytest.mark.parametrize(
    "routes",
    [
        {"missing": ["backup"]},
        {"primary": ["missing"]},
        {"": ["backup"]},
        {"primary": [""]},
        {"primary": [" "]},
        {" ": ["backup"]},
        {"primary": []},
        {"primary": ["backup", "backup"]},
        {"primary": ["primary"]},
        {"primary": ["backup"], "backup": ["second"]},
        {"primary": ["backup"], "backup": ["primary"]},
    ],
)
def test_invalid_approval_graph_is_rejected_with_useful_error(routes):
    data = config_data()
    data["llm_fallbacks"] = routes
    with pytest.raises((ValidationError, ValueError)) as caught:
        CouncilSettings.model_validate(data)
    assert str(caught.value).strip(), f"F03 useful diagnostic for {routes=}"


def test_route_references_are_checked_even_with_empty_backend_registry():
    with pytest.raises((ValidationError, ValueError)):
        CouncilSettings(llm_backends={}, llm_fallbacks={"primary": ["backup"]})


@pytest.mark.parametrize(
    "location,value",
    [
        ("primary.sdk_max_retries", 1),
        ("backup.sdk_max_retries", 1),
        ("second.sdk_max_retries", 2),
        ("llm_max_retries", 0),
        ("llm_max_retries", 1),
        ("llm_retry_budget_seconds", float("inf")),
        ("llm_retry_budget_seconds", float("nan")),
        ("backup.request_timeout_seconds", float("inf")),
        ("primary.request_timeout_seconds", float("nan")),
    ],
)
def test_configured_routes_cannot_multiply_attempts_or_have_nonfinite_times(location, value):
    data = config_data()
    if "." in location:
        backend, field = location.split(".")
        data["llm_backends"][backend][field] = value
    else:
        data[location] = value
    with pytest.raises((ValidationError, ValueError)):
        CouncilSettings.model_validate(data)


def test_unused_definition_is_not_approval_or_preflight_dependency():
    data = config_data()
    data["llm_backends"]["unused"].update(api_key="", sdk_max_retries=4)
    data["redis"] = {
        "password": "qa-generated-redis-secret",  # pragma: allowlist secret - inert offline fixture
        "account_integrity_key": "qa-integrity",
    }
    data["nat"] = {"host": "127.0.0.1"}
    cfg = CouncilSettings.model_validate(data)
    validate_council_deployment_preflight(cfg, profile="alpha")
    assert cfg.llm_backends["unused"].sdk_max_retries == 4, "F01 unused remains legacy"


@pytest.mark.parametrize("role", ["member", "chairman", "code_review"])
def test_reachable_backup_credentials_fail_alpha_preflight_for_every_role(role):
    data = copy.deepcopy(config_data())
    data["members"] = []
    if role == "member":
        data["members"] = [{"id": "m", "backend": "primary", "priorities": ["Review"]}]
    else:
        data[role] = {"enabled": True, "backend": "primary"}
    data["redis"] = {
        "password": "qa-generated-redis-secret",  # pragma: allowlist secret - inert offline fixture
        "account_integrity_key": "qa-integrity",
    }
    data["nat"] = {"host": "127.0.0.1"}
    data["llm_backends"]["backup"]["api_key"] = ""
    cfg = CouncilSettings.model_validate(data)
    with pytest.raises(ValueError, match=r"(?i)backup.*api_key|api_key.*backup"):
        validate_council_deployment_preflight(cfg, profile="alpha")
