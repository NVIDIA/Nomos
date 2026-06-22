# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


"""
Pydantic validation models for executor.yaml configuration.

These models validate the YAML config at load time, providing clear
error messages for invalid values, bad regex patterns, and missing
required fields.
"""

import re

from pydantic import BaseModel, Field, field_validator, model_validator


class ExecutorSection(BaseModel):
    """HTTP server and execution settings."""

    listen_host: str = "127.0.0.1"
    listen_port: int = Field(default=9902, ge=1, le=65535)
    log_level: str = "INFO"
    max_command_timeout: int = Field(default=60, ge=1, le=3600)
    identity_private_key_path: str = Field(
        default="",
        description="Writable path for the executor-owned Ed25519 evidence signing key.",
    )
    identity_public_key_path: str = Field(
        default="",
        description="Optional path where the executor writes its raw public key bytes.",
    )
    passthrough_env: list[str] = Field(default_factory=list)
    injected_env: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Environment variables (including secrets from host)"
            " unconditionally injected into every execution."
        ),
    )

    # ── Advanced Authorization & Constraints ──
    require_valid_token: bool = Field(
        default=True, description="Require a valid, unexpired token for execution"
    )
    require_agent_signature: bool = Field(
        default=False, description="Require the agent to cryptographically sign the command payload"
    )
    enforce_rate_limits: bool = Field(
        default=True, description="Enforce rate limiting on execution requests"
    )
    max_requests_per_minute: int = Field(default=60, ge=1)
    max_concurrent_executions: int = Field(default=10, ge=1)
    allow_interactive_sessions: bool = Field(
        default=False, description="Allow commands that require TTY/interactive input"
    )

    @field_validator("passthrough_env")
    @classmethod
    def validate_passthrough_env(cls, v: list[str]) -> list[str]:
        """Reject common secret variable names to prevent accidental leakage."""
        secret_substrings = {"SECRET", "TOKEN", "PASSWORD", "PASSWD", "CREDENTIAL"}
        secret_suffixes = {"_KEY"}
        # Exact names that are safe despite matching suffix patterns
        safe_exact = {"EXECUTOR_PUBLIC_KEY_PATH"}
        for name in v:
            upper = name.upper()
            if upper in safe_exact:
                continue
            for sub in secret_substrings:
                if sub in upper:
                    raise ValueError(
                        f"passthrough_env entry {name!r} looks like a secret "
                        f"(contains {sub!r}). Secrets must not be forwarded to "
                        f"subprocesses — commands can read them via env/printenv."
                    )
            for suffix in secret_suffixes:
                if upper.endswith(suffix):
                    raise ValueError(
                        f"passthrough_env entry {name!r} looks like a secret "
                        f"(ends with {suffix!r}). Secrets must not be forwarded to "
                        f"subprocesses — commands can read them via env/printenv."
                    )
        return v

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v.upper() not in allowed:
            raise ValueError(f"log_level must be one of {allowed}, got {v!r}")
        return v.upper()


class CouncilSection(BaseModel):
    """Council connection settings."""

    public_key_path: str = ""
    url: str = ""
    key_fetch_retries: int = Field(default=10, ge=1, le=100)
    key_fetch_delay: int = Field(default=3, ge=1, le=60)

    @model_validator(mode="after")
    def at_least_one_key_source(self) -> "CouncilSection":
        # Validation is lenient here — env vars may override at runtime
        return self


class RedisSection(BaseModel):
    """Redis connection settings."""

    mode: str = "standalone"
    url: str = "redis://localhost:6379/0"
    key_prefix: str = Field(default="nomos:", min_length=1)
    sentinel_service_name: str = ""
    sentinel_nodes: list[str] = Field(default_factory=list)
    sentinel_password: str = ""
    connect_timeout_ms: int = Field(default=1000, ge=1)
    command_timeout_ms: int = Field(default=1000, ge=1)

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        mode = v.strip().lower()
        if mode not in {"standalone", "sentinel"}:
            raise ValueError("Redis mode must be standalone or sentinel")
        return mode

    @field_validator("url")
    @classmethod
    def validate_redis_url(cls, v: str) -> str:
        if not v.startswith(("redis://", "rediss://")):
            raise ValueError(f"Redis URL must start with redis:// or rediss://, got {v!r}")
        return v

    @model_validator(mode="after")
    def validate_topology(self) -> "RedisSection":
        if self.mode == "sentinel":
            if not self.sentinel_service_name:
                raise ValueError("sentinel_service_name is required in sentinel mode")
            if not self.sentinel_nodes:
                raise ValueError("sentinel_nodes is required in sentinel mode")
        return self


class FilesystemSection(BaseModel):
    """Filesystem access controls."""

    allowed_working_dirs: list[str] = Field(default_factory=lambda: ["/workspace"])
    denied_paths: list[str] = Field(default_factory=list)

    @field_validator("allowed_working_dirs")
    @classmethod
    def validate_working_dirs(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("allowed_working_dirs must not be empty")
        for d in v:
            if not d.startswith("/"):
                raise ValueError(f"Working directory must be absolute path, got {d!r}")
        return v

    @field_validator("denied_paths")
    @classmethod
    def validate_denied_paths(cls, v: list[str]) -> list[str]:
        for p in v:
            if not p or not p.strip():
                raise ValueError("denied_paths entries must be non-empty strings")
        return v


class ToolsSection(BaseModel):
    """Tool whitelist and per-tool argument restrictions."""

    allowed: list[str] = Field(default_factory=list)
    denied_args: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("allowed")
    @classmethod
    def validate_allowed_tools(cls, v: list[str]) -> list[str]:
        for tool in v:
            if not tool or not tool.strip():
                raise ValueError("Tool names must be non-empty strings")
            if "/" in tool:
                raise ValueError(f"Tool names should be basenames, not paths: {tool!r}")
        return v

    @field_validator("denied_args")
    @classmethod
    def validate_denied_args(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        for tool_name, patterns in v.items():
            if not tool_name.strip():
                raise ValueError("Tool name in denied_args must be non-empty")
            for i, pattern in enumerate(patterns):
                try:
                    re.compile(pattern)
                except re.error as e:
                    raise ValueError(
                        f"Invalid regex in denied_args[{tool_name!r}][{i}]: {pattern!r} — {e}"
                    ) from e
        return v


class SecuritySection(BaseModel):
    """Security patterns and settings."""

    blocked_patterns: list[str] = Field(default_factory=list)

    @field_validator("blocked_patterns")
    @classmethod
    def validate_blocked_patterns(cls, v: list[str]) -> list[str]:
        for i, pattern in enumerate(v):
            try:
                re.compile(pattern)
            except re.error as e:
                raise ValueError(
                    f"Invalid regex in blocked_patterns[{i}]: {pattern!r} — {e}"
                ) from e
        return v


class TelemetrySection(BaseModel):
    """Telemetry and observability settings."""

    enabled: bool = Field(default=True, description="Enable metrics and tracing")
    prometheus_port: int = Field(
        default=9090, ge=1, le=65535, description="Port for Prometheus metrics exposure"
    )
    otlp_endpoint: str = Field(default="", description="Endpoint for OpenTelemetry tracing")


class AuthSection(BaseModel):
    """Authentication and identity verification settings."""

    allowed_issuers: list[str] = Field(
        default_factory=lambda: ["nomos-council"], description="Valid token issuers"
    )
    required_audience: str = Field(
        default="writ-executor", description="Required audience claim in tokens"
    )
    token_leeway_seconds: int = Field(
        default=30, ge=0, description="Clock skew allowance for token validation"
    )


class OpenShellSection(BaseModel):
    """OpenShell backend settings for the openshell executor package."""

    endpoint: str = ""
    request_timeout: int = Field(default=30, ge=1, le=3600)


class ExecutorYamlConfig(BaseModel):
    """Top-level Pydantic model for executor.yaml.

    Validates the entire config file at load time. All sections are
    optional — missing sections use restrictive defaults.
    """

    executor: ExecutorSection = Field(default_factory=ExecutorSection)
    council: CouncilSection = Field(default_factory=CouncilSection)
    redis: RedisSection = Field(default_factory=RedisSection)
    filesystem: FilesystemSection = Field(default_factory=FilesystemSection)
    tools: ToolsSection = Field(default_factory=ToolsSection)
    security: SecuritySection = Field(default_factory=SecuritySection)
    telemetry: TelemetrySection = Field(default_factory=TelemetrySection)
    auth: AuthSection = Field(default_factory=AuthSection)
    openshell: OpenShellSection = Field(default_factory=OpenShellSection)

    model_config = {"extra": "forbid"}
