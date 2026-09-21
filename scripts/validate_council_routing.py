#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Validate council backend routing for public or internal deployments."""

from __future__ import annotations

import argparse
from pathlib import Path
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_COUNCIL_FILES = (
    ROOT / "configs" / "council.yaml",
    ROOT / "deploy" / "kubernetes" / "nomos" / "files" / "council" / "council.yaml",
)
NV_INFERENCE_ENDPOINT = "inference-api.nvidia.com"
ROUTING_MODES = ("public", "internal")


class CouncilConfigValidationError(ValueError):
    """Raised when the routing guard cannot safely inspect a council config."""


def _string_mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CouncilConfigValidationError(f"{label} must be a mapping")

    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise CouncilConfigValidationError(f"{label} keys must be strings")
        result[key] = item
    return result


def nv_inference_routes(path: Path) -> list[str]:
    """Return active backend entries that use nv_inference routing."""
    try:
        raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise CouncilConfigValidationError(str(exc)) from exc

    root = _string_mapping(raw, "root")
    backends = _string_mapping(root.get("llm_backends"), "llm_backends")
    findings: list[str] = []
    for name, value in backends.items():
        config = _string_mapping(value, f"llm_backends.{name}")
        provider = config.get("provider")
        if not isinstance(provider, str) or not provider:
            raise CouncilConfigValidationError(f"llm_backends.{name}.provider must be a string")
        base_url = config.get("base_url", "")
        if not isinstance(base_url, str):
            raise CouncilConfigValidationError(f"llm_backends.{name}.base_url must be a string")
        try:
            hostname = (urlsplit(base_url).hostname or "").lower().rstrip(".")
        except ValueError as exc:
            raise CouncilConfigValidationError(
                f"llm_backends.{name}.base_url is invalid: {exc}"
            ) from exc
        if provider == "nv_inference":
            findings.append(f"{name}: provider is nv_inference")
        if hostname == NV_INFERENCE_ENDPOINT:
            findings.append(f"{name}: base_url uses {NV_INFERENCE_ENDPOINT}")
    return findings


def routing_violations(path: Path, mode: str) -> list[str]:
    """Return backend routing entries that violate the selected deployment mode."""
    if mode not in ROUTING_MODES:
        raise ValueError(f"unsupported routing mode: {mode}")

    try:
        raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise CouncilConfigValidationError(str(exc)) from exc

    root = _string_mapping(raw, "root")
    backends = _string_mapping(root.get("llm_backends"), "llm_backends")
    findings: list[str] = []
    for name, value in backends.items():
        config = _string_mapping(value, f"llm_backends.{name}")
        provider = config.get("provider")
        if not isinstance(provider, str) or not provider:
            raise CouncilConfigValidationError(f"llm_backends.{name}.provider must be a string")
        base_url = config.get("base_url", "")
        if not isinstance(base_url, str):
            raise CouncilConfigValidationError(f"llm_backends.{name}.base_url must be a string")
        try:
            parsed_url = urlsplit(base_url)
            hostname = (parsed_url.hostname or "").lower().rstrip(".")
            _ = parsed_url.port
        except ValueError as exc:
            raise CouncilConfigValidationError(
                f"llm_backends.{name}.base_url is invalid: {exc}"
            ) from exc

        if mode == "public":
            if provider == "nv_inference":
                findings.append(f"{name}: provider is nv_inference")
            if hostname == NV_INFERENCE_ENDPOINT:
                findings.append(f"{name}: base_url uses {NV_INFERENCE_ENDPOINT}")
        else:
            if provider != "nv_inference":
                findings.append(f"{name}: provider must be nv_inference")
            if hostname != NV_INFERENCE_ENDPOINT:
                findings.append(f"{name}: base_url must use {NV_INFERENCE_ENDPOINT}")
            elif parsed_url.scheme.lower() != "https":
                findings.append(f"{name}: base_url must use HTTPS")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=ROUTING_MODES, required=True)
    parser.add_argument("paths", nargs="*", type=Path, default=PUBLIC_COUNCIL_FILES)
    args = parser.parse_args()

    failed = False
    routing_failed = False
    for path in args.paths:
        try:
            findings = routing_violations(path, args.mode)
        except CouncilConfigValidationError as exc:
            failed = True
            print(f"{path}: invalid council config: {exc}")
            continue
        for finding in findings:
            failed = True
            routing_failed = True
            print(f"{path}: {finding}")
    if routing_failed and args.mode == "public":
        print(
            "Public council configs must not use the nv_inference provider or "
            f"{NV_INFERENCE_ENDPOINT} endpoint."
        )
    elif routing_failed:
        print(
            "Internal council configs must use the nv_inference provider and "
            f"{NV_INFERENCE_ENDPOINT} endpoint for every backend."
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
