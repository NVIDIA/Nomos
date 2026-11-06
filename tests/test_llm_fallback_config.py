# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Engineer regressions for explicit operator route approval."""

import math

import pytest
from pydantic import ValidationError

from nomos.config.settings import CouncilSettings, load_council_catalog


def route_settings(**updates):
    values = {
        "llm_backends": {
            name: {"provider": "openai", "model": name} for name in ("primary", "backup", "last")
        },
        "llm_fallbacks": {"primary": ["backup", "last"]},
    }
    values.update(updates)
    return CouncilSettings(**values)


def test_fallback_is_explicit_opt_in():
    assert CouncilSettings().llm_fallbacks == {}
    assert route_settings().llm_fallbacks == {"primary": ["backup", "last"]}


@pytest.mark.parametrize(
    "route",
    [
        {"primary": []},
        {"primary": ["primary"]},
        {"primary": ["backup", "backup"]},
        {"missing": ["backup"]},
        {"primary": ["missing"]},
        {"": ["backup"]},
        {"primary": ["backup"], "backup": ["last"]},
        {"primary": ["backup"], "backup": ["primary"]},
    ],
)
def test_invalid_approval_graph_rejected(route):
    with pytest.raises(ValidationError, match="llm_fallbacks"):
        route_settings(llm_fallbacks=route)


@pytest.mark.parametrize("limit", [0, 1])
def test_approval_cannot_expand_existing_attempt_limit(limit):
    with pytest.raises(ValidationError, match="attempt limit"):
        route_settings(llm_max_retries=limit)


@pytest.mark.parametrize("bad", [math.inf, math.nan])
def test_configured_budget_must_be_finite(bad):
    with pytest.raises(ValidationError):
        route_settings(llm_retry_budget_seconds=bad)


def test_sdk_retries_and_infinite_candidate_timeout_are_rejected():
    for override in ({"sdk_max_retries": 1}, {"request_timeout_seconds": math.inf}):
        settings = route_settings().model_dump()
        settings["llm_backends"]["backup"].update(override)
        with pytest.raises(ValidationError, match="llm_fallbacks"):
            CouncilSettings(**settings)


def test_empty_registry_does_not_bypass_approval_validation():
    with pytest.raises(ValidationError, match="unknown backend"):
        CouncilSettings(llm_fallbacks={"primary": ["backup"]})


def test_catalog_resolves_approvals_per_council(tmp_path):
    import yaml

    path = tmp_path / "engineer-catalog.yaml"
    shared = route_settings().model_dump(mode="json")
    shared.pop("council_id")
    path.write_text(
        yaml.safe_dump(
            {
                "shared": shared,
                "councils": {
                    "one": {},
                    "two": {"llm_fallbacks": {"primary": ["last"]}},
                },
            }
        )
    )
    catalog = load_council_catalog(path)
    assert catalog.councils["one"].llm_fallbacks["primary"] == ["backup", "last"]
    assert catalog.councils["two"].llm_fallbacks["primary"] == ["last"]
