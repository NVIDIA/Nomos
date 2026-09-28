# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Check that generated local NIM profiles preserve policy and route every role."""

import importlib.util
from pathlib import Path

import pytest
import yaml
from nomos.config.settings import CouncilSettings


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/scripts/render-self-hosted-council.py"
SPEC = importlib.util.spec_from_file_location("render_self_hosted_council", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
SOURCE = (ROOT / "configs/council.yaml").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("mode", "local_roles", "expected_backends"),
    [
        ("pure", set(MODULE.ROLES), {"nim-local"}),
        (
            "mixed",
            set(MODULE.DEFAULT_MIXED_LOCAL_ROLES),
            {"nim-local", "nim-hosted"},
        ),
        (
            "mixed",
            {"environment-isolation", "blast-containment"},
            {"nim-local", "nim-hosted"},
        ),
    ],
)
def test_generated_profile_routes_roles_without_changing_policy(
    mode, local_roles, expected_backends, monkeypatch
):
    original = yaml.safe_load(SOURCE)
    rendered = yaml.safe_load(
        MODULE.render(
            SOURCE,
            local_model="local/model-id",
            local_url="http://nim-llm:8000/v1",
            mode=mode,
            local_roles=local_roles,
        )
    )

    if mode == "mixed":
        monkeypatch.setenv("NVIDIA_API_KEY", "test-hosted-key")
    else:
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.setenv("COUNCIL_REDIS_HOST", "redis")
    monkeypatch.setenv("COUNCIL_REDIS_PASSWORD", "test-redis-password")
    monkeypatch.setenv("COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY", "test-integrity-key")
    CouncilSettings.model_validate(rendered)

    assert set(rendered["llm_backends"]) == expected_backends
    assert rendered["llm_backends"]["nim-local"]["model"] == "local/model-id"
    assert rendered["llm_backends"]["nim-local"]["api_key"] == "local-nim"
    if mode == "mixed":
        assert rendered["llm_backends"]["nim-hosted"]["api_key"] == "${NVIDIA_API_KEY}"

    for member in rendered["members"]:
        expected = "nim-local" if member["id"] in local_roles else "nim-hosted"
        assert member["backend"] == expected
    for role in ("code_review", "chairman"):
        expected = "nim-local" if role in local_roles else "nim-hosted"
        assert rendered[role]["backend"] == expected

    for role in ("members", "code_review", "chairman"):
        original.pop(role)
        rendered.pop(role)
    original.pop("llm_backends")
    rendered.pop("llm_backends")
    assert rendered == original


def test_mixed_profile_rejects_all_local_roles():
    with pytest.raises(ValueError, match="at least one local and one hosted"):
        MODULE.render(
            SOURCE,
            local_model="local/model-id",
            local_url="http://nim-llm:8000/v1",
            mode="mixed",
            local_roles=set(MODULE.ROLES),
        )
