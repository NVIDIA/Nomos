# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Configuration settings for Nomos.

Pydantic-based settings for LLM backends, council members,
and the overall council deployment.
"""

import copy
import os
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, Field, field_validator, model_validator

from nomos_runtime import BindCheck, SecretCheck, validate_deployment_preflight


def _resolve_env_reference(value: Any, default: Any = "") -> Any:
    """Resolve simple ${VAR} or ${VAR:-default} references used in YAML config."""
    if not isinstance(value, str):
        return value
    if not (value.startswith("${") and value.endswith("}")):
        return value

    expression = value[2:-1]
    if ":-" in expression:
        env_var, fallback = expression.split(":-", 1)
    else:
        env_var, fallback = expression, default
    resolved = os.environ.get(env_var)
    return fallback if resolved in (None, "") else resolved


class LLMProvider(StrEnum):
    """Supported LLM providers."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    NIM = "nim"
    NV_INFERENCE = "nv_inference"


class LLMBackendConfig(BaseModel):
    """Configuration for a single LLM backend."""

    provider: LLMProvider
    model: str
    api_key: str = Field(
        default="", description="API key (use env var reference like ${OPENAI_API_KEY})"
    )
    base_url: str = Field(default="", description="Custom API base URL")
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1024, ge=1)
    max_concurrent: int = Field(
        default=4,
        ge=1,
        description=(
            "Maximum in-flight requests to the endpoint this backend talks to. "
            "Shared across all backends and councils targeting the same "
            "(provider, base_url); size to provider_limit / council_replicas"
        ),
    )
    request_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        description="Per-request timeout passed to the provider SDK client",
    )
    sdk_max_retries: int = Field(
        default=0,
        ge=0,
        description=(
            "Retries performed inside the provider SDK client. Default 0 so the "
            "council's own retry loop is the single retry authority"
        ),
    )
    overload_latency_threshold_seconds: float = Field(
        default=120.0,
        ge=0,
        description=(
            "Fast-fail new council work when a new request's projected response "
            "time (queue drain + one call, from the endpoint limiter's latency "
            "EWMA) exceeds this many seconds. Queue depth never matters by "
            "itself. 0 disables shedding"
        ),
    )

    @field_validator("api_key", mode="before")
    @classmethod
    def resolve_env_var(cls, v: str) -> str:
        """Resolve environment variable references like ${VAR_NAME}."""
        if v.startswith("${") and v.endswith("}"):
            env_var = v[2:-1]
            resolved = os.environ.get(env_var, "")
            if not resolved:
                raise ValueError(f"Environment variable {env_var} not set")
            return resolved
        return v


class CouncilMemberConfig(BaseModel):
    """
    Configuration for a single council member agent.

    Each member has an LLM backend and a set of priorities that
    shape how they evaluate intents. The system prompt is built
    from these priorities.
    """

    id: str = Field(..., min_length=1, description="Unique member identifier")
    backend: str = Field(..., description="Name of the LLM backend to use (key in llm_backends)")
    weight: int = Field(default=1, ge=1, description="Voting weight")
    display_name: str = Field(default="", description="Human-readable name")

    # ── Advanced Auth & Scoping ──
    require_mfa: bool = Field(
        default=False,
        description="Require human Multi-Factor Authentication for this member to approve",
    )
    require_cryptographic_auth: bool = Field(
        default=False, description="Require the agent to cryptographically sign their approval"
    )
    allowed_environments: list[str] = Field(
        default_factory=lambda: ["*"],
        description="Environments this member is authorized to review (e.g., ['dev', 'staging'])",
    )
    max_risk_level: str = Field(
        default="critical",
        description="Maximum risk level this member can approve (low, medium, high, critical)",
    )
    auto_approve_patterns: list[str] = Field(
        default_factory=list,
        description=(
            "Regex patterns for commands this member can auto-approve without LLM evaluation"
        ),
    )
    cooldown_seconds: int = Field(
        default=0,
        ge=0,
        description="Minimum seconds between approvals for the same target by this member",
    )

    priorities: list[str] = Field(
        default_factory=list,
        min_length=1,
        description="Ordered list of evaluation priorities",
    )
    system_prompt_override: str = Field(
        default="",
        description="Optional full system prompt override (bypasses priority-based generation)",
    )
    evaluation_criteria: dict[str, str] = Field(
        default_factory=dict,
        description="Named criteria with descriptions for structured evaluation",
    )

    def build_system_prompt(self) -> str:
        """
        Build the system prompt from priorities and criteria.

        Returns
        -------
        str
            The complete system prompt for the council member.
        """
        if self.system_prompt_override:
            return self.system_prompt_override

        prompt_parts = [
            "You are a council member reviewing privileged action"
            " requests in a multi-agent system.",
            "Your role is to evaluate whether an intent"
            " (a proposed action) should be APPROVED or DENIED.",
            "",
            "You will receive the full intent including the exact command, target environment,",
            "context, justification, and blast radius. Evaluate it carefully.",
            "",
            "Evidence has trust levels. Certified executor evidence has already been",
            "validated by the council service against a registered trusted executor",
            "public key: the signature, executor key fingerprint, signed command/result",
            "payload, and stdout/stderr hashes matched. Treat that as verified output",
            "from the named trusted executor, under the deployment assumption that the",
            "executor private key is secure. Plain evidence, description, justification,",
            "context, ticket numbers, and blast-radius prose are agent-asserted claims;",
            "use them as context, but do not confuse them with certified evidence.",
            "",
            "If the intent includes Packaged Files, those files are first-class",
            "request artifacts, not missing prose. Their bytes, path, mode, and",
            "SHA-256 digest are carried in the structured intent and, if approved,",
            "are bound into the signed writ. The Code Review council member receives",
            "the decoded file contents before you vote. Use the Code Review Analysis",
            "as the file-content evidence; do not require the requester to paste the",
            "full file contents into the description, justification, or context solely",
            "because a packaged-file workflow is used. Deny if Code Review is absent,",
            "denies, is inconclusive, or if the reviewed behavior still conflicts with",
            "the command, scope, blast radius, rollback, or your own priorities.",
            "",
        ]

        if self.priorities:
            prompt_parts.append("YOUR PRIORITIES (in order of importance):")
            for i, priority in enumerate(self.priorities, 1):
                prompt_parts.append(f"  {i}. {priority}")
            prompt_parts.append("")

        if self.evaluation_criteria:
            prompt_parts.append("EVALUATION CRITERIA:")
            for name, desc in self.evaluation_criteria.items():
                prompt_parts.append(f"  - {name}: {desc}")
            prompt_parts.append("")

        prompt_parts.extend(
            [
                "RESPONSE FORMAT:",
                "Respond with a single JSON object and nothing else.",
                "{",
                '  "decision": "approve",',
                '  "reasoning": "REQUIRED — at least two full sentences stating'
                " the specific evidence from the intent that drove your decision."
                " Never empty. Never the string 'null'. Never 'see above'.\",",
                '  "risk_concerns": ["list of specific concerns if any"],',
                '  "conditions": ["conditions that must be met for approval, if any"],',
                '  "guidance": "if denying, explain how the request'
                ' could be narrowed or improved to gain approval"',
                "}",
                "",
                "REASONING IS MANDATORY. A response with an empty, missing, null,",
                "or whitespace-only reasoning field is a protocol violation and",
                "will be rejected. Every decision — approve OR deny — must cite",
                "concrete evidence from the intent fields (description,",
                "justification, command, target, blast radius, rollback plan,",
                "and clearly labeled agent-asserted or certified evidence).",
                "",
                "Allowed values:",
                '- "decision" must be exactly "approve" or "deny".',
                "",
                "Be rigorous and security-conscious, but do not deny solely because",
                "a request uses a powerful tool or because incidental business metadata",
                "cannot be independently verified. Approve well-scoped operational work",
                "when the command, scope, and rollback evidence are sufficient.",
                "If you deny, provide actionable guidance on how the request could be",
                "narrowed or evidenced to become acceptable.",
            ]
        )

        return "\n".join(prompt_parts)


class NATConfig(BaseModel):
    """Configuration for NAT (NeMo Agent Toolkit) integration."""

    host: str = Field(default="localhost", description="MCP server host")
    port: int = Field(default=9901, ge=1, le=65535, description="MCP server port")
    transport: str = Field(default="streamable-http", description="MCP transport type")
    server_name: str = Field(default="Nomos Council", description="MCP server name")

    @field_validator("host", "transport", "server_name", mode="before")
    @classmethod
    def resolve_string_env_vars(cls, value: Any) -> Any:
        return _resolve_env_reference(value, "")

    @field_validator("port", mode="before")
    @classmethod
    def resolve_port_env_var(cls, value: Any) -> Any:
        return _resolve_env_reference(value, 9901)


class RedisConfig(BaseModel):
    """Configuration for Redis connection."""

    mode: str = Field(
        default="${COUNCIL_REDIS_MODE:-standalone}",
        validate_default=True,
        description="Redis topology: standalone or sentinel",
    )
    host: str = Field(
        default="${COUNCIL_REDIS_HOST:-localhost}",
        validate_default=True,
        description="Redis host",
    )
    port: int = Field(default=6379, ge=1, le=65535, description="Redis port")
    db: int = Field(default=0, ge=0, description="Redis database number")
    password: str = Field(
        default="${COUNCIL_REDIS_PASSWORD:-}",
        validate_default=True,
        description="Redis password (use ${ENV_VAR} syntax)",
    )
    tls: bool = Field(default=False, description="Use TLS for Redis connection")
    key_prefix: str = Field(default="nomos:", description="Namespace prefix for all Redis keys")
    sentinel_service_name: str = Field(
        default="${COUNCIL_REDIS_SENTINEL_SERVICE_NAME:-}",
        validate_default=True,
        description="Redis Sentinel master name",
    )
    sentinel_nodes: list[str] = Field(
        default="${COUNCIL_REDIS_SENTINEL_NODES:-}",
        validate_default=True,
        description="Sentinel host:port nodes",
    )
    sentinel_password: str = Field(
        default="${COUNCIL_REDIS_SENTINEL_PASSWORD:-}",
        validate_default=True,
        description="Optional Sentinel auth password",
    )
    connect_timeout_ms: int = Field(default=1000, ge=1, description="Redis connect timeout")
    command_timeout_ms: int = Field(default=1000, ge=1, description="Redis command timeout")
    account_integrity_key: str = Field(
        default="${COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY:-}",
        validate_default=True,
        description="Optional HMAC key for Redis account-critical state",
    )

    @field_validator(
        "mode",
        "host",
        "password",
        "sentinel_service_name",
        "sentinel_password",
        "account_integrity_key",
        mode="before",
    )
    @classmethod
    def resolve_env_vars(cls, value: Any, info) -> str:
        """Resolve environment variable references like ${VAR}."""
        default = "localhost" if info.field_name == "host" else ""
        resolved = _resolve_env_reference(value, default)
        if not resolved and info.field_name == "host":
            return "localhost"
        return cast(str, resolved)

    @field_validator("port", "db", "connect_timeout_ms", "command_timeout_ms", mode="before")
    @classmethod
    def resolve_int_env_vars(cls, v: Any, info) -> Any:
        defaults = {
            "port": 6379,
            "db": 0,
            "connect_timeout_ms": 1000,
            "command_timeout_ms": 1000,
        }
        return _resolve_env_reference(v, defaults[info.field_name])

    @field_validator("tls", mode="before")
    @classmethod
    def resolve_bool_env_vars(cls, v: Any) -> Any:
        return _resolve_env_reference(v, False)

    @field_validator("sentinel_nodes", mode="before")
    @classmethod
    def resolve_sentinel_nodes(cls, v: Any) -> Any:
        resolved = _resolve_env_reference(v, "")
        if isinstance(resolved, str):
            return [item.strip() for item in resolved.split(",") if item.strip()]
        return resolved

    @field_validator("mode")
    @classmethod
    def normalize_mode(cls, value: str) -> str:
        mode = value.strip().lower()
        if mode not in {"standalone", "sentinel"}:
            raise ValueError("redis.mode must be 'standalone' or 'sentinel'")
        return mode

    @model_validator(mode="after")
    def validate_topology(self) -> "RedisConfig":
        if self.mode == "sentinel":
            if not self.sentinel_service_name:
                raise ValueError("redis.sentinel_service_name is required in sentinel mode")
            if not self.sentinel_nodes:
                raise ValueError("redis.sentinel_nodes is required in sentinel mode")
        return self


class StorageConfig(BaseModel):
    """Durable storage backend selection.

    mode "redis" (default) keeps the status quo: Redis holds both durable and
    ephemeral state. mode "postgres" makes PostgreSQL the durable source of
    truth (accounts, writs, intents, ledger) while Redis keeps only nonces,
    rate counters, and cache — the shape required for multi-replica HA.
    """

    mode: str = Field(
        default="${COUNCIL_STORAGE_MODE:-redis}",
        validate_default=True,
        description="Durable storage backend: redis or postgres",
    )
    dsn: str = Field(
        default="${COUNCIL_DATABASE_URL:-}",
        validate_default=True,
        description="PostgreSQL connection DSN",
    )
    pool_min_size: int = Field(default=1, ge=0, description="Async store pool minimum")
    pool_max_size: int = Field(default=10, ge=1, description="Async store pool maximum")
    ledger_pool_min_size: int = Field(default=1, ge=0, description="Ledger sync pool minimum")
    ledger_pool_max_size: int = Field(default=4, ge=1, description="Ledger sync pool maximum")
    connect_timeout_s: float = Field(default=5.0, gt=0, description="Connection timeout")
    statement_timeout_ms: int = Field(
        default=5000, ge=1, description="Per-statement timeout inside ledger transactions"
    )
    lock_timeout_ms: int = Field(
        default=2000, ge=1, description="Row/advisory lock acquisition timeout"
    )
    append_max_retries: int = Field(
        default=5, ge=0, description="Ledger append retry attempts before failing closed"
    )
    run_migrations_on_startup: bool = Field(
        default=True, description="Apply pending schema migrations at service startup"
    )

    @field_validator("mode", "dsn", mode="before")
    @classmethod
    def resolve_env_vars(cls, value: Any) -> str:
        return cast(str, _resolve_env_reference(value, ""))

    @field_validator("mode")
    @classmethod
    def normalize_mode(cls, value: str) -> str:
        mode = value.strip().lower() or "redis"
        if mode not in {"redis", "postgres"}:
            raise ValueError("storage.mode must be 'redis' or 'postgres'")
        return mode

    @model_validator(mode="after")
    def validate_postgres_requirements(self) -> "StorageConfig":
        if self.mode == "postgres" and not self.dsn:
            raise ValueError(
                "storage.dsn (or COUNCIL_DATABASE_URL) is required when storage.mode=postgres"
            )
        return self


class SigningConfig(BaseModel):
    """Signer provider selection.

    provider "local" (default) keeps Ed25519 keys on disk/in memory via the
    existing CouncilKeyManager paths. provider "vault" signs through the Vault
    transit engine so private key bytes never reach the pod filesystem.
    Env names match the deployment contract (app-contract-v1).
    """

    provider: str = Field(
        default="${NOMOS_SIGNER_PROVIDER:-local}",
        validate_default=True,
        description="Signer backend: local or vault",
    )
    council_key_id: str = Field(
        default="${COUNCIL_SIGNER_KEY_ID:-}",
        validate_default=True,
        description="Vault transit key name used for council writ/ledger signing",
    )
    vault_addr: str = Field(
        default="${NOMOS_VAULT_ADDR:-}",
        validate_default=True,
        description="Vault server address (https://...)",
    )
    vault_namespace: str = Field(
        default="${NOMOS_VAULT_NAMESPACE:-}",
        validate_default=True,
        description="Optional Vault enterprise namespace",
    )
    vault_transit_mount: str = Field(
        default="${NOMOS_VAULT_TRANSIT_MOUNT:-transit}",
        validate_default=True,
        description="Mount path of the transit secrets engine",
    )
    vault_k8s_role: str = Field(
        default="${NOMOS_VAULT_K8S_ROLE:-}",
        validate_default=True,
        description="Vault kubernetes-auth role (preferred auth when set)",
    )
    vault_k8s_token_path: str = Field(
        default="${NOMOS_VAULT_K8S_TOKEN_PATH:-/var/run/secrets/nomos/vault-token}",
        validate_default=True,
        description="Projected service-account JWT path for kubernetes auth",
    )
    vault_role_id: str = Field(
        default="${VAULT_ROLE_ID:-}",
        validate_default=True,
        description="AppRole role_id fallback auth",
    )
    vault_secret_id: str = Field(
        default="${VAULT_SECRET_ID:-}",
        validate_default=True,
        description="AppRole secret_id fallback auth",
    )
    timeout_s: float = Field(default=5.0, gt=0, description="Vault HTTP timeout")

    @field_validator(
        "provider",
        "council_key_id",
        "vault_addr",
        "vault_namespace",
        "vault_transit_mount",
        "vault_k8s_role",
        "vault_k8s_token_path",
        "vault_role_id",
        "vault_secret_id",
        mode="before",
    )
    @classmethod
    def resolve_env_vars(cls, value: Any, info) -> str:
        defaults = {
            "provider": "local",
            "vault_transit_mount": "transit",
            "vault_k8s_token_path": "/var/run/secrets/nomos/vault-token",
        }
        return cast(str, _resolve_env_reference(value, defaults.get(info.field_name, "")))

    @field_validator("provider")
    @classmethod
    def normalize_provider(cls, value: str) -> str:
        provider = value.strip().lower() or "local"
        if provider not in {"local", "vault"}:
            raise ValueError("signing.provider must be 'local' or 'vault'")
        return provider

    @model_validator(mode="after")
    def validate_vault_requirements(self) -> "SigningConfig":
        if self.provider == "vault":
            if not self.vault_addr:
                raise ValueError("signing.vault_addr (NOMOS_VAULT_ADDR) is required for vault")
            if not self.council_key_id:
                raise ValueError(
                    "signing.council_key_id (COUNCIL_SIGNER_KEY_ID) is required for vault"
                )
        return self


class ChairmanConfig(BaseModel):
    """
    Configuration for the council chairman.

    The chairman does not vote. They assess risk and decide the approval
    threshold. Unanimous approval is the default; quorum is honored only
    when allow_quorum is explicitly enabled.
    """

    enabled: bool = Field(default=False, description="Whether to use a chairman")
    backend: str = Field(default="", description="Name of the LLM backend to use")
    system_prompt_override: str = Field(
        default="", description="Override the default chairman system prompt"
    )
    allow_quorum: bool = Field(
        default=False,
        description=(
            "Allow the chairman to choose quorum. Defaults false so unanimous is the "
            "baseline unless quorum is explicitly enabled in deployment config."
        ),
    )
    parallel_member_evaluation: bool = Field(
        default=True,
        description=(
            "Evaluate the chairman and initial voting members concurrently. Set false to "
            "restore the legacy chairman-then-submit-then-members ordering."
        ),
    )


class CodeReviewConfig(BaseModel):
    """Configuration for packaged-file code review."""

    enabled: bool = Field(default=False, description="Review packaged files before council voting")
    backend: str = Field(default="", description="Name of the LLM backend to use")
    display_name: str = Field(default="Code Review", description="Human-readable reviewer name")
    max_file_bytes: int = Field(
        default=131072,
        ge=1024,
        le=1048576,
        description="Maximum bytes per packaged file included in code review prompts",
    )
    system_prompt_override: str = Field(
        default="", description="Override the default code review system prompt"
    )


class OPAConfig(BaseModel):
    """Configuration for Open Policy Agent intent filtering."""

    enabled: bool = Field(default=True, description="Evaluate intents with OPA before council")
    url: str = Field(default="http://localhost:8181", description="OPA base URL")
    decision_path: str = Field(
        default="/v1/data/nomos/intent/decision",
        description="OPA Data API path or package-relative decision path",
    )
    timeout_ms: int = Field(default=500, ge=1, le=60000, description="OPA request timeout")
    fail_closed: bool = Field(
        default=True,
        description="Deny intents when OPA is enabled but cannot return a decision",
    )
    policy_id: str = Field(
        default="strict-shell",
        description=(
            "Named bundled OPA policy profile. strict-shell requires absolute "
            "executables; git-workflow-local and read-only-relaxed may permit "
            "configured bare command words under configured working directories."
        ),
    )
    relaxed_bare_command_words: list[str] = Field(
        default_factory=list,
        description=(
            "Additional non-absolute shell command words this council allows OPA to "
            "pass through when the selected policy profile supports relaxation."
        ),
    )
    relaxed_working_dir_prefixes: list[str] = Field(
        default_factory=list,
        description=(
            "Absolute working-directory prefixes where relaxed bare command words may "
            "be accepted by OPA for this council."
        ),
    )

    @field_validator("enabled", "fail_closed", mode="before")
    @classmethod
    def resolve_bool_env_vars(cls, value: Any, info) -> Any:
        default = True
        return _resolve_env_reference(value, default)

    @field_validator("url", mode="before")
    @classmethod
    def resolve_url_env_vars(cls, value: Any) -> Any:
        return _resolve_env_reference(value, "http://localhost:8181")

    @field_validator("decision_path", mode="before")
    @classmethod
    def resolve_decision_path_env_vars(cls, value: Any) -> Any:
        return _resolve_env_reference(value, "/v1/data/nomos/intent/decision")

    @field_validator("timeout_ms", mode="before")
    @classmethod
    def resolve_timeout_env_vars(cls, value: Any) -> Any:
        return _resolve_env_reference(value, 500)

    @field_validator("policy_id", mode="before")
    @classmethod
    def resolve_policy_id_env_vars(cls, value: Any) -> Any:
        return _resolve_env_reference(value, "strict-shell")

    @field_validator("url")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        url = value.strip().rstrip("/")
        if not url:
            raise ValueError("OPA url must not be empty")
        if not (url.startswith("http://") or url.startswith("https://")):
            raise ValueError("OPA url must start with http:// or https://")
        return url

    @field_validator("decision_path")
    @classmethod
    def normalize_decision_path(cls, value: str) -> str:
        path = value.strip()
        if not path:
            raise ValueError("OPA decision_path must not be empty")
        if path.startswith("/v1/data/"):
            return path
        return f"/v1/data/{path.strip('/')}"

    @field_validator("policy_id")
    @classmethod
    def normalize_policy_id(cls, value: str) -> str:
        policy_id = value.strip()
        if not policy_id:
            raise ValueError("OPA policy_id must not be empty")
        return policy_id

    @field_validator("relaxed_bare_command_words")
    @classmethod
    def normalize_relaxed_command_words(cls, value: list[str]) -> list[str]:
        normalized = sorted({item.strip() for item in value if item.strip()})
        for word in normalized:
            if "/" in word:
                raise ValueError("OPA relaxed_bare_command_words must be command words, not paths")
        return normalized

    @field_validator("relaxed_working_dir_prefixes")
    @classmethod
    def normalize_relaxed_working_dir_prefixes(cls, value: list[str]) -> list[str]:
        normalized = sorted({item.strip().rstrip("/") for item in value if item.strip()})
        for prefix in normalized:
            if not prefix.startswith("/"):
                raise ValueError("OPA relaxed_working_dir_prefixes must be absolute paths")
        return normalized


class TelemetryConfig(BaseModel):
    """Internal telemetry delivery for monitor-only council latency metrics."""

    enabled: bool = Field(default=False, description="Send internal latency telemetry")
    url: str = Field(default="", description="Monitor internal telemetry endpoint")
    token: str = Field(default="", description="Shared internal telemetry token")
    timeout_ms: int = Field(default=250, ge=1, le=5000, description="Telemetry POST timeout")
    # Redis stream sink (the post-gateway stats path: council is the sole producer,
    # the monitor reads the stream). Independent of the HTTP sink above.
    redis_enabled: bool = Field(default=True, description="Mirror telemetry to a Redis stream")
    redis_stream: str = Field(
        default="council:telemetry",
        description="Redis stream key (prefixed with the redis key_prefix)",
    )
    redis_maxlen: int = Field(
        default=5000, ge=100, le=1_000_000, description="Approx MAXLEN cap for the stream"
    )

    @field_validator("enabled", "redis_enabled", mode="before")
    @classmethod
    def resolve_enabled_env_var(cls, value: Any) -> Any:
        return _resolve_env_reference(value, False)

    @field_validator("url", "token", "redis_stream", mode="before")
    @classmethod
    def resolve_string_env_vars(cls, value: Any) -> Any:
        return _resolve_env_reference(value, "")

    @field_validator("timeout_ms", "redis_maxlen", mode="before")
    @classmethod
    def resolve_timeout_env_var(cls, value: Any) -> Any:
        return _resolve_env_reference(value, 250)


class AuditConfig(BaseModel):
    """Configuration for security auditing and logging."""

    enabled: bool = Field(default=True, description="Enable audit logging")
    log_format: str = Field(default="json", description="Format for audit logs (json, text)")
    destinations: list[str] = Field(
        default_factory=lambda: ["console"],
        description="Where to send audit logs (console, file, webhook, syslog)",
    )
    file_path: str = Field(
        default="/var/log/nomos/audit.log",
        description="Path to audit log file if 'file' destination is used",
    )
    webhook_url: str = Field(
        default="", description="Webhook URL for SIEM integration if 'webhook' destination is used"
    )


class AgentAuthConfig(BaseModel):
    """Global configuration for agent authentication."""

    require_agent_signatures: bool = Field(
        default=False, description="Require agents to cryptographically sign their requests"
    )
    token_rotation_interval: int = Field(
        default=3600, ge=60, description="How often agent tokens must be rotated (seconds)"
    )
    max_tokens_per_agent: int = Field(
        default=5, ge=1, description="Maximum number of active tokens a single agent can hold"
    )
    allowed_agent_ips: list[str] = Field(
        default_factory=lambda: ["*"], description="IP allowlist for agent connections"
    )


class CouncilSettings(BaseModel):
    """
    Top-level configuration for the Nomos council.

    This is the single configuration object that defines everything
    needed to deploy the council: LLM backends, council members,
    policy, and NAT/MCP settings.
    """

    council_id: str = Field(default="council-default", description="Unique council identifier")
    enabled: bool = Field(default=True, description="Whether this council can receive requests")
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Operator metadata for dynamic councils and generated profiles",
    )
    require_justification: bool = Field(
        default=True, description="Require justification on all intents"
    )
    default_token_ttl_seconds: int = Field(default=300, ge=1)
    max_pending_intents: int = Field(default=1000, ge=1)
    llm_max_retries: int = Field(
        default=2,
        ge=0,
        description="Retries per LLM agent call (rate-limit and parse errors)",
    )
    llm_retry_budget_seconds: float = Field(
        default=40.0,
        gt=0,
        description="Wall-clock cap per agent evaluation including retries",
    )
    llm_max_retry_after_seconds: float = Field(
        default=10.0,
        ge=0,
        description="Cap on honored Retry-After sleeps from the provider",
    )

    audit: AuditConfig = Field(
        default_factory=AuditConfig, description="Audit logging configuration"
    )
    agent_auth: AgentAuthConfig = Field(
        default_factory=AgentAuthConfig, description="Agent authentication configuration"
    )

    llm_backends: dict[str, LLMBackendConfig] = Field(
        default_factory=dict,
        description="Named LLM backend configurations",
    )
    members: list[CouncilMemberConfig] = Field(
        default_factory=list,
        description="Council member configurations",
    )
    chairman: ChairmanConfig = Field(
        default_factory=ChairmanConfig, description="Council chairman configuration"
    )
    code_review: CodeReviewConfig = Field(
        default_factory=CodeReviewConfig,
        description="Optional pre-vote code reviewer for packaged files",
    )
    opa: OPAConfig = Field(
        default_factory=OPAConfig,
        description="Optional OPA filter evaluated before council review",
    )
    telemetry: TelemetryConfig = Field(
        default_factory=TelemetryConfig,
        description="Internal monitor telemetry for council latency metrics",
    )
    require_auth: bool = Field(
        default=True, description="Fail-closed: reject all requests when Redis/auth is unavailable"
    )
    require_council_membership: bool = Field(
        default=True,
        description=(
            "Require authenticated agents to be members of this council before "
            "they may petition it."
        ),
    )
    allow_uncertified_evidence: bool = Field(
        default="${COUNCIL_ALLOW_UNCERTIFIED_EVIDENCE:-true}",
        validate_default=True,
        description=(
            "Allow plain string evidence submitted by agents. When false, evidence must be "
            "provided as executor-certified evidence and verified against a trusted executor key."
        ),
    )
    council_key_path: str = Field(
        default="/app/keys", description="Directory for council Ed25519 key pair files"
    )
    ledger_path: str = Field(
        default="",
        description="Optional JSONL path for persistent council ledger storage",
    )
    redis: RedisConfig = Field(
        default_factory=RedisConfig, description="Redis connection configuration"
    )
    storage: StorageConfig = Field(
        default_factory=StorageConfig,
        description="Durable storage backend selection (redis or postgres)",
    )
    signing: SigningConfig = Field(
        default_factory=SigningConfig,
        description="Signer provider selection (local key files or Vault transit)",
    )

    nat: NATConfig = Field(default_factory=NATConfig, description="NAT/MCP server settings")

    @field_validator("allow_uncertified_evidence", mode="before")
    @classmethod
    def resolve_allow_uncertified_evidence_env_var(cls, value: Any) -> Any:
        return _resolve_env_reference(value, True)

    @field_validator("members")
    @classmethod
    def validate_member_backends(
        cls, v: list[CouncilMemberConfig], info
    ) -> list[CouncilMemberConfig]:
        """Validate that all members reference existing backends."""
        backends = info.data.get("llm_backends", {})
        if backends:
            for member in v:
                if member.backend not in backends:
                    raise ValueError(
                        f"Council member '{member.id}' references"
                        f" unknown backend '{member.backend}'."
                        f" Available backends: {list(backends.keys())}"
                    )
        return v

    @model_validator(mode="after")
    def validate_code_review_backend(self) -> "CouncilSettings":
        if (
            self.code_review.enabled
            and self.code_review.backend
            and self.llm_backends
            and self.code_review.backend not in self.llm_backends
        ):
            raise ValueError(
                f"Code review references unknown backend {self.code_review.backend!r}. "
                f"Available backends: {list(self.llm_backends.keys())}"
            )
        return self


class CouncilCatalogSettings(BaseModel):
    """Resolved deployment catalog for one or more councils."""

    default_council_id: str
    councils: dict[str, CouncilSettings]

    def get(self, council_id: str | None = None) -> CouncilSettings:
        selected = council_id or self.default_council_id
        try:
            return self.councils[selected]
        except KeyError as exc:
            raise KeyError(
                f"Unknown council_id {selected!r}. Known councils: {sorted(self.councils)}"
            ) from exc


def validate_council_deployment_preflight(
    settings: CouncilSettings,
    *,
    profile: str | None = None,
) -> None:
    """Run deployment-profile checks for council settings.

    The default profile is development, so this is non-breaking unless operators
    explicitly opt into Alpha behavior with NOMOS_DEPLOY_PROFILE=alpha.
    """

    selected_profile = profile if profile is not None else os.environ.get("NOMOS_DEPLOY_PROFILE")
    allow_public_bind = os.environ.get("NOMOS_ALPHA_ALLOW_PUBLIC_BIND", "").lower() in {
        "1",
        "true",
        "yes",
    }
    active_backend_ids = {member.backend for member in settings.members}
    if settings.chairman.enabled and settings.chairman.backend:
        active_backend_ids.add(settings.chairman.backend)
    if settings.code_review.enabled and settings.code_review.backend:
        active_backend_ids.add(settings.code_review.backend)

    secrets = [
        SecretCheck("redis.password", settings.redis.password),
        SecretCheck("redis.account_integrity_key", settings.redis.account_integrity_key),
    ]
    for backend_id in sorted(active_backend_ids):
        backend = settings.llm_backends.get(backend_id)
        if backend is not None:
            secrets.append(SecretCheck(f"llm_backends.{backend_id}.api_key", backend.api_key))

    validate_deployment_preflight(
        profile=selected_profile,
        secrets=secrets,
        binds=[BindCheck("nat.host", settings.nat.host, allow_public=allow_public_bind)],
    )


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Return a recursive merge where override values win."""
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _load_raw_config(path: str) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(config_path) as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"Config file {path} must contain a YAML mapping")
    return raw


def _normalize_catalog_council_runtime(
    council_raw: dict[str, Any],
    *,
    server_nat: dict[str, Any] | None,
) -> dict[str, Any]:
    """Normalize fields that are process-level when councils share one MCP server."""
    if server_nat is None:
        return council_raw
    normalized = copy.deepcopy(council_raw)
    profile_nat = normalized.get("nat")
    if profile_nat is None:
        normalized["nat"] = copy.deepcopy(server_nat)
    elif isinstance(profile_nat, dict):
        normalized["nat"] = _deep_merge(profile_nat, server_nat)
    else:
        raise ValueError("council nat config must be a mapping when provided")
    return normalized


def load_council_catalog(path: str) -> CouncilCatalogSettings:
    """Load one or more council definitions from YAML.

    Backward-compatible single-council files are normalized to a catalog
    containing one council. Multi-council files use:

    ```yaml
    default_council_id: council-prod
    shared:
      redis: ...
      llm_backends: ...
    councils:
      council-prod:
        members: [...]
      council-infra:
        members: [...]
    ```
    """
    raw = _load_raw_config(path)
    base_dir = Path(path).resolve().parent

    if "councils" not in raw:
        settings = CouncilSettings.model_validate(raw)
        validate_council_deployment_preflight(settings)
        return CouncilCatalogSettings(
            default_council_id=settings.council_id,
            councils={settings.council_id: settings},
        )

    server_nat = raw.get("nat")
    if server_nat is not None and not isinstance(server_nat, dict):
        raise ValueError("catalog-level 'nat' must be a mapping when provided")

    councils_raw = raw.get("councils")
    if not isinstance(councils_raw, dict) or not councils_raw:
        raise ValueError("multi-council config requires a non-empty 'councils' mapping")

    shared = raw.get("shared", {})
    if shared is None:
        shared = {}
    if not isinstance(shared, dict):
        raise ValueError("'shared' must be a mapping when provided")

    councils: dict[str, CouncilSettings] = {}
    for council_id, council_raw in councils_raw.items():
        if not isinstance(council_id, str) or not council_id:
            raise ValueError("council ids must be non-empty strings")
        if council_raw is None:
            council_raw = {}
        if not isinstance(council_raw, dict):
            raise ValueError(f"council {council_id!r} must be a mapping")
        config_ref = council_raw.get("config_file")
        if config_ref:
            if not isinstance(config_ref, str):
                raise ValueError(f"council {council_id!r} config_file must be a string")
            ref_path = Path(config_ref)
            if not ref_path.is_absolute():
                ref_path = base_dir / ref_path
            referenced = _load_raw_config(str(ref_path))
            overrides = {k: v for k, v in council_raw.items() if k != "config_file"}
            council_raw = _deep_merge(referenced, overrides)
        council_raw = _normalize_catalog_council_runtime(
            council_raw,
            server_nat=server_nat,
        )
        resolved = _deep_merge(shared, council_raw)
        resolved["council_id"] = resolved.get("council_id") or council_id
        if resolved["council_id"] != council_id:
            raise ValueError(
                f"council map key {council_id!r} does not match council_id "
                f"{resolved['council_id']!r}"
            )
        settings = CouncilSettings.model_validate(resolved)
        validate_council_deployment_preflight(settings)
        councils[council_id] = settings

    default_id = raw.get("default_council_id") or next(iter(councils))
    if default_id not in councils:
        raise ValueError(
            f"default_council_id {default_id!r} is not defined in councils: {sorted(councils)}"
        )
    return CouncilCatalogSettings(default_council_id=default_id, councils=councils)


def load_config(path: str) -> CouncilSettings:
    """
    Load council configuration from a YAML file.

    The YAML structure mirrors the CouncilSettings model:

    ```yaml
    council_id: "my-council"
    require_justification: true

    llm_backends:
      nim-main:
        provider: nim
        model: nvidia/nemotron-3-ultra-550b-a55b
        api_key: "${NVIDIA_API_KEY}"
        base_url: "https://integrate.api.nvidia.com/v1"
        max_tokens: 8192

    members:
      - id: security-reviewer
        backend: nim-main
        weight: 1
        priorities:
          - "Evaluate security implications and privilege escalation risks"
          - "Verify blast radius is accurately described"

    nat:
      host: "0.0.0.0"
      port: 9901
    ```
    """
    return load_council_catalog(path).get()
