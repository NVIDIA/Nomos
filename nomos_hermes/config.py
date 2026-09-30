# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Trusted deployment configuration for the Hermes adapter."""

from __future__ import annotations

import math
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from trusted_executor_contract.authorization import MAX_TRUSTED_KEYS


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _validated_mcp_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("NOMOS_MCP_URL must not contain credentials or a fragment")
    if parsed.scheme == "https" and parsed.hostname:
        return value
    allow_loopback = os.environ.get("NOMOS_HERMES_ALLOW_INSECURE_LOOPBACK", "").lower() in {
        "1",
        "true",
        "yes",
    }
    if (
        allow_loopback
        and parsed.scheme == "http"
        and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        return value
    raise ValueError("NOMOS_MCP_URL must use HTTPS")


def _load_public_keys(path_text: str) -> tuple[bytes, ...]:
    path = Path(path_text)
    # Kubernetes projects Secret and ConfigMap files through a symlink beneath
    # the declared mount directory. Permit that standard layout, but do not let
    # a configured link escape its deployment-owned mount.
    mount_dir = path.parent.resolve(strict=True)
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(mount_dir):
        raise ValueError("Council public-key file escapes its configured mount")
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Council public-key path must be a regular file")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError("Council public-key file must not be group/world writable")
    if info.st_size > 8192:
        raise ValueError("Council public-key file is too large")
    keys: list[bytes] = []
    for raw_line in resolved.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            key = bytes.fromhex(line)
        except ValueError as exc:
            raise ValueError("Council public-key file contains invalid hexadecimal data") from exc
        if len(key) != 32:
            raise ValueError("Council public keys must be 32-byte Ed25519 keys")
        keys.append(key)
    if not keys or len(keys) > MAX_TRUSTED_KEYS or len(set(keys)) != len(keys):
        raise ValueError("Council public-key file contains an invalid key set")
    return tuple(keys)


@dataclass(frozen=True)
class HermesNomosConfig:
    """Configuration resolved once while Hermes loads the immutable plugin."""

    enabled: bool
    mcp_url: str = ""
    auth_token: str = field(default="", repr=False)
    transport_token: str = field(default="", repr=False)
    agent_id: str = ""
    council_id: str = ""
    council_public_keys: tuple[bytes, ...] = ()
    timeout_seconds: float = 90.0
    max_writ_lifetime_seconds: float = 300.0
    clock_skew_seconds: float = 5.0

    @classmethod
    def from_env(cls) -> HermesNomosConfig:
        """Load and validate the fail-closed Hermes adapter configuration."""
        enabled_text = os.environ.get("NOMOS_HERMES_ENABLED", "").strip().lower()
        # Scrub credentials even when disabled or malformed so no later tool
        # subprocess inherits accidentally supplied Nomos secrets.
        auth_token = os.environ.pop("NOMOS_AUTH_TOKEN", "").strip()
        transport_token = os.environ.pop("NOMOS_MCP_BEARER_TOKEN", "").strip()
        if enabled_text in {"", "0", "false", "no"}:
            return cls(enabled=False)
        if enabled_text not in {"1", "true", "yes"}:
            raise ValueError("NOMOS_HERMES_ENABLED must be an explicit boolean")
        if not auth_token:
            raise ValueError("NOMOS_AUTH_TOKEN is required")
        timeout = float(os.environ.get("NOMOS_HERMES_TIMEOUT_SECONDS", "90"))
        max_lifetime = float(os.environ.get("NOMOS_HERMES_MAX_WRIT_LIFETIME_SECONDS", "300"))
        clock_skew = float(os.environ.get("NOMOS_HERMES_CLOCK_SKEW_SECONDS", "5"))
        if (
            not all(math.isfinite(value) for value in (timeout, max_lifetime, clock_skew))
            or timeout <= 0
            or max_lifetime <= 0
            or clock_skew < 0
        ):
            raise ValueError("Nomos Hermes time bounds are invalid")
        return cls(
            enabled=True,
            mcp_url=_validated_mcp_url(_required("NOMOS_MCP_URL")),
            auth_token=auth_token,
            transport_token=transport_token,
            agent_id=_required("NOMOS_AGENT_ID"),
            council_id=_required("NOMOS_COUNCIL_ID"),
            council_public_keys=_load_public_keys(_required("NOMOS_COUNCIL_PUBLIC_KEYS_FILE")),
            timeout_seconds=timeout,
            max_writ_lifetime_seconds=max_lifetime,
            clock_skew_seconds=clock_skew,
        )
