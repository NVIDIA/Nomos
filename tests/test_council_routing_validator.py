# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for public and internal council routing validation."""

from __future__ import annotations

import sys

import pytest

from scripts.validate_council_routing import (
    CouncilConfigValidationError,
    main,
    nv_inference_routes,
    routing_violations,
)


def test_public_mode_accepts_public_config_and_commented_nv_inference_example(tmp_path):
    path = tmp_path / "council.yaml"
    path.write_text(
        """llm_backends:
  public:
    provider: nim
    base_url: https://integrate.api.nvidia.com/v1
# nv-inference:
#   provider: nv_inference
#   base_url: https://inference-api.nvidia.com/v1
"""
    )

    assert nv_inference_routes(path) == []
    assert routing_violations(path, "public") == []


def test_public_mode_rejects_active_nv_inference_provider(tmp_path):
    path = tmp_path / "council.yaml"
    path.write_text("llm_backends:\n  nv-inference:\n    provider: nv_inference\n")

    assert routing_violations(path, "public") == ["nv-inference: provider is nv_inference"]


@pytest.mark.parametrize(
    "base_url",
    (
        "https://inference-api.nvidia.com",
        "https://inference-api.nvidia.com/v1",
        "https://INFERENCE-API.NVIDIA.COM/v1",
    ),
)
def test_public_mode_rejects_active_nv_inference_endpoint(tmp_path, base_url):
    path = tmp_path / "council.yaml"
    path.write_text(
        f"llm_backends:\n  nv-inference:\n    provider: nim\n    base_url: {base_url}\n"
    )

    assert routing_violations(path, "public") == [
        "nv-inference: base_url uses inference-api.nvidia.com"
    ]


def test_internal_mode_accepts_nv_inference_routing(tmp_path):
    path = tmp_path / "council.yaml"
    path.write_text(
        """llm_backends:
  nv-sonnet:
    provider: nv_inference
    base_url: https://inference-api.nvidia.com/v1
"""
    )

    assert routing_violations(path, "internal") == []


@pytest.mark.parametrize(
    ("provider", "base_url", "message"),
    (
        (
            "nim",
            "https://inference-api.nvidia.com/v1",
            "nv-sonnet: provider must be nv_inference",
        ),
        (
            "nv_inference",
            "https://integrate.api.nvidia.com/v1",
            "nv-sonnet: base_url must use inference-api.nvidia.com",
        ),
    ),
)
def test_internal_mode_rejects_non_internal_routing(tmp_path, provider, base_url, message):
    path = tmp_path / "council.yaml"
    path.write_text(
        f"llm_backends:\n  nv-sonnet:\n    provider: {provider}\n    base_url: {base_url}\n"
    )

    assert routing_violations(path, "internal") == [message]


@pytest.mark.parametrize(
    ("contents", "message"),
    (
        ("[]\n", "root must be a mapping"),
        ("llm_backends: []\n", "llm_backends must be a mapping"),
        (
            "llm_backends:\n  malformed: not-a-mapping\n",
            "llm_backends.malformed must be a mapping",
        ),
        (
            "llm_backends:\n  malformed:\n    provider: 7\n",
            "llm_backends.malformed.provider must be a string",
        ),
        (
            "llm_backends:\n  malformed:\n    provider: nim\n    base_url: 7\n",
            "llm_backends.malformed.base_url must be a string",
        ),
    ),
)
def test_validator_rejects_malformed_config(tmp_path, contents, message):
    path = tmp_path / "council.yaml"
    path.write_text(contents)

    with pytest.raises(CouncilConfigValidationError, match=message):
        routing_violations(path, "public")


def test_main_reports_invalid_config_without_traceback(tmp_path, monkeypatch, capsys):
    path = tmp_path / "council.yaml"
    path.write_text("llm_backends: [\n")
    monkeypatch.setattr(sys, "argv", ["validate_council_routing.py", str(path)])

    assert main() == 1

    output = capsys.readouterr()
    assert output.out.startswith(f"{path}: invalid council config:")
    assert "Traceback" not in output.out
    assert "must not use the nv_inference provider" not in output.out


def test_main_accepts_internal_mode(tmp_path, monkeypatch, capsys):
    path = tmp_path / "council.yaml"
    path.write_text(
        "llm_backends:\n"
        "  nv-sonnet:\n"
        "    provider: nv_inference\n"
        "    base_url: https://inference-api.nvidia.com/v1\n"
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["validate_council_routing.py", "--mode", "internal", str(path)],
    )

    assert main() == 0
    assert capsys.readouterr().out == ""
