# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Validate a local, single-platform Docker runtime without reserving model requests."""

import json
import re
import subprocess


def validate_image_id(image: str):
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ValueError("runtime_image must be an immutable image ID, not a registry reference")


def inspect_image(image: str) -> str:
    validate_image_id(image)
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    values = json.loads(result.stdout)
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
        raise ValueError("Docker must return exactly one runtime image")
    value = values[0]
    if value.get("Id") != image:
        raise ValueError("Docker runtime image identity differs from the frozen ID")
    descriptor = value.get("Descriptor")
    if descriptor is not None and (
        not isinstance(descriptor, dict)
        or descriptor.get("mediaType")
        not in {
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        }
    ):
        raise ValueError("runtime must be a single-platform image; rebuild with --provenance=false")
    platform = [value.get("Os"), value.get("Architecture")]
    if value.get("Variant"):
        platform.append(value["Variant"])
    if any(not isinstance(part, str) or not part for part in platform):
        raise ValueError("Docker runtime image lacks OS/architecture metadata")
    return "/".join(platform)
