# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the public council configuration routing safeguard."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from nomos.config.settings import CouncilSettings, LLMProvider
from scripts.validate_council_routing import nv_inference_routes

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_CONFIGS = (
    ROOT / "configs/council.yaml",
    ROOT / "deploy/kubernetes/nomos/files/council/council.yaml",
)


EXPECTED_MODELS = {
    "nim-kimi": "moonshotai/kimi-k3",
    "nim-deepseek": "deepseek-ai/deepseek-v4-flash-0731",
    "nim-nemotron": "nvidia/nemotron-3-ultra-550b-a55b",
    "anthropic-opus": "claude-opus-4-6",
    "anthropic-sonnet": "claude-sonnet-4-6",
    "openai-gpt": "gpt-5.5",
}

EXPECTED_REQUEST_TIMEOUTS = {
    "nim-kimi": 120,
    "nim-deepseek": 60,
    "nim-nemotron": 120,
}


@pytest.fixture
def config_credentials(monkeypatch):
    for key in ("NVIDIA_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(key, "test-provider-key")
    monkeypatch.setenv("COUNCIL_REDIS_HOST", "redis")
    monkeypatch.setenv("COUNCIL_REDIS_PASSWORD", "test-redis-password")


def test_shipped_council_templates_match():
    """Compare complete release templates; runtime profiles have their own lifecycle."""
    assert yaml.safe_load(PUBLIC_CONFIGS[0].read_text()) == yaml.safe_load(
        PUBLIC_CONFIGS[1].read_text()
    )


@pytest.mark.parametrize("path", PUBLIC_CONFIGS)
def test_shipped_default(path, config_credentials):
    config = CouncilSettings.model_validate(yaml.safe_load(path.read_text()))
    assert set(config.llm_backends) == {"nim-kimi"}
    assert config.llm_backends["nim-kimi"].model == EXPECTED_MODELS["nim-kimi"]
    assert (
        config.llm_backends["nim-kimi"].request_timeout_seconds
        == EXPECTED_REQUEST_TIMEOUTS["nim-kimi"]
    )
    assert len(config.members) == 3
    assert {member.backend for member in config.members} == {"nim-kimi"}
    assert config.chairman.backend == config.code_review.backend == "nim-kimi"
    assert config.chairman.allow_quorum is False


@pytest.mark.parametrize("path", PUBLIC_CONFIGS)
@pytest.mark.parametrize(
    ("example", "backends", "review_backend"),
    [
        ("MIXED", ("nim-kimi", "nim-deepseek", "nim-nemotron"), "nim-kimi"),
        ("DIRECT VENDOR", ("anthropic-opus", "anthropic-sonnet", "openai-gpt"), "anthropic-opus"),
    ],
)
def test_shipped_profiles_and_commented_alternative(
    path, example, backends, review_backend, config_credentials, tmp_path
):
    """Protect documented choices and policy; compatibility uses separate probes."""
    source = path.read_text()
    raw = yaml.safe_load(source)
    original = CouncilSettings.model_validate(raw)
    start = f"  # BEGIN OPTIONAL {example} BACKENDS"
    end = f"  # END OPTIONAL {example} BACKENDS"
    assert source.count(start) == source.count(end) == 1
    block = source.split(start)[1].split(end)[0]
    alternative = yaml.safe_load(
        "\n".join(line.removeprefix("  # ") for line in block.splitlines())
    )
    if example == "MIXED":
        assert set(alternative) == set(backends) - {"nim-kimi"}
        raw["llm_backends"].update(alternative)
    else:
        assert set(alternative) == set(backends)
        raw["llm_backends"] = alternative
    assignments = dict(
        zip(
            ("command-integrity", "environment-isolation", "blast-containment"),
            backends,
            strict=True,
        )
    )
    for member in raw["members"]:
        member["backend"] = assignments[member["id"]]
    for role in ("chairman", "code_review"):
        raw[role]["backend"] = review_backend
    activated = CouncilSettings.model_validate(raw)
    activated_path = tmp_path / f"{path.parent.name}-{example.lower().replace(' ', '-')}.yaml"
    activated_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    assert nv_inference_routes(activated_path) == []
    assert all(
        backend.provider != LLMProvider.NV_INFERENCE
        and "inference-api.nvidia.com" not in backend.base_url.lower()
        for backend in activated.llm_backends.values()
    )
    assert {name: backend.model for name, backend in activated.llm_backends.items()} == {
        name: EXPECTED_MODELS[name] for name in backends
    }
    for name in set(backends) & set(EXPECTED_REQUEST_TIMEOUTS):
        assert (
            activated.llm_backends[name].request_timeout_seconds == EXPECTED_REQUEST_TIMEOUTS[name]
        )
    for member in activated.members:
        assert member.backend == assignments[member.id]
        assert member.backend in activated.llm_backends
    assert activated.chairman.backend == activated.code_review.backend == review_backend
    assert review_backend in activated.llm_backends
    # Every reviewer setting except the intentionally switched backend is preserved.
    for before, after in zip(original.members, activated.members, strict=True):
        assert before.model_dump(exclude={"backend"}) == after.model_dump(exclude={"backend"})
    for role in ("chairman", "code_review"):
        assert getattr(original, role).model_dump(exclude={"backend"}) == getattr(
            activated, role
        ).model_dump(exclude={"backend"})
