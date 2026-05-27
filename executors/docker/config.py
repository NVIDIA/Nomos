"""
Executor configuration.

Minimal config for the writ executor service. No dependency on nomos.
All security policy (blocked patterns, denied paths, allowed tools, denied
tool args) is driven entirely by the YAML config file — there are no
hardcoded defaults.
"""

import logging
import os
import re
from dataclasses import dataclass, field

_LOGGER = logging.getLogger(__name__)


@dataclass
class ExecutorConfig:
    """Configuration for the writ executor service."""

    # Council public key — file path OR council URL (fetched on startup)
    council_public_key_path: str = ""
    council_url: str = ""  # e.g. "http://council:9901"

    # Redis connection (for nonce store and outcome reporting)
    redis_mode: str = "standalone"
    redis_url: str = "redis://localhost:6379/0"
    redis_key_prefix: str = "nomos:"
    redis_sentinel_service_name: str = ""
    redis_sentinel_nodes: list[str] = field(default_factory=list)
    redis_sentinel_password: str = ""
    redis_connect_timeout_ms: int = 1000
    redis_command_timeout_ms: int = 1000

    # HTTP server
    listen_host: str = "127.0.0.1"
    listen_port: int = 9902

    # Executor-owned identity used to sign execution evidence
    executor_private_key_path: str = ""
    executor_public_key_path: str = ""

    # Security — all driven by YAML config, no hardcoded defaults
    allowed_working_directories: list[str] = field(default_factory=lambda: ["/workspace"])
    max_command_timeout: int = 60  # seconds — tight default
    blocked_patterns: list[re.Pattern] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)  # empty = allow-all
    denied_paths: list[str] = field(default_factory=list)
    denied_tool_args: dict[str, list[re.Pattern]] = field(default_factory=dict)

    # Environment passthrough — these env vars from the executor process
    # are forwarded to subprocess execution (e.g. KUBECONFIG, HOME)
    passthrough_env: list[str] = field(default_factory=list)

    # Hardcoded environment variables to inject (e.g., {"AWS_REGION": "us-east-1"})
    # Can reference host env vars if loaded via YAML with ${VAR} syntax
    injected_env: dict[str, str] = field(default_factory=dict)

    # Logging
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "ExecutorConfig":
        """Load config from environment variables (legacy / fallback).

        WARNING: env-only mode has NO security policy — no blocked patterns,
        no denied paths, no tool whitelist. Use YAML config for production.
        """
        allowed_dirs_str = os.environ.get("EXECUTOR_ALLOWED_DIRS", "")
        if allowed_dirs_str:
            allowed_dirs = [d.strip() for d in allowed_dirs_str.split(":") if d.strip()]
        else:
            allowed_dirs = ["/workspace"]

        patterns = _compile_blocked_patterns(os.environ.get("EXECUTOR_BLOCKED_PATTERNS", ""))

        return cls(
            council_public_key_path=os.environ.get("EXECUTOR_PUBLIC_KEY_PATH", ""),
            council_url=os.environ.get("EXECUTOR_COUNCIL_URL", ""),
            redis_mode=os.environ.get("EXECUTOR_REDIS_MODE", "standalone"),
            redis_url=os.environ.get("EXECUTOR_REDIS_URL", "redis://localhost:6379/0"),
            redis_key_prefix=os.environ.get("EXECUTOR_REDIS_PREFIX", "nomos:"),
            redis_sentinel_service_name=os.environ.get("EXECUTOR_REDIS_SENTINEL_SERVICE_NAME", ""),
            redis_sentinel_nodes=_split_csv(os.environ.get("EXECUTOR_REDIS_SENTINEL_NODES", "")),
            redis_sentinel_password=os.environ.get("EXECUTOR_REDIS_SENTINEL_PASSWORD", ""),
            redis_connect_timeout_ms=int(
                os.environ.get("EXECUTOR_REDIS_CONNECT_TIMEOUT_MS", "1000")
            ),
            redis_command_timeout_ms=int(
                os.environ.get("EXECUTOR_REDIS_COMMAND_TIMEOUT_MS", "1000")
            ),
            listen_host=os.environ.get("EXECUTOR_HOST", "127.0.0.1"),
            listen_port=int(os.environ.get("EXECUTOR_PORT", "9902")),
            executor_private_key_path=os.environ.get("EXECUTOR_PRIVATE_KEY_PATH", ""),
            executor_public_key_path=os.environ.get("EXECUTOR_PUBLIC_KEY_EXPORT_PATH", ""),
            allowed_working_directories=allowed_dirs,
            max_command_timeout=int(os.environ.get("EXECUTOR_MAX_TIMEOUT", "60")),
            log_level=os.environ.get("EXECUTOR_LOG_LEVEL", "INFO"),
            blocked_patterns=patterns,
            allowed_tools=[],
            denied_paths=[],
            denied_tool_args={},
            passthrough_env=[],
        )

    @classmethod
    def from_yaml(cls, path: str) -> "ExecutorConfig":
        """Load config from a YAML file with Pydantic validation.

        All security policy comes from the YAML file — no hardcoded defaults.
        Falls back to from_env() if the file doesn't exist.
        Environment variables still override YAML values for:
          EXECUTOR_PUBLIC_KEY_PATH, EXECUTOR_REDIS_URL, EXECUTOR_HOST,
          EXECUTOR_PORT, EXECUTOR_LOG_LEVEL
        """
        import yaml
        from pydantic import ValidationError

        from .config_schema import ExecutorYamlConfig

        config_path = os.path.expanduser(path)
        if not os.path.isfile(config_path):
            raise FileNotFoundError(
                f"Executor config file not found: {config_path}\n"
                f"EXECUTOR_CONFIG is set but the file does not exist. "
                f"Fix the path or unset EXECUTOR_CONFIG to use env-var fallback."
            )

        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}

        # Validate through Pydantic — raises clear errors on bad config
        try:
            validated = ExecutorYamlConfig.model_validate(raw)
        except ValidationError as e:
            raise ValueError(f"Invalid executor config at {config_path}:\n{e}") from e

        _validate_unimplemented_fields(validated, config_path)

        # Blocked patterns: YAML only, no hardcoded defaults
        patterns = _compile_blocked_patterns(validated.security.blocked_patterns)

        # Allowed dirs from YAML, env override wins
        allowed_dirs_str = os.environ.get("EXECUTOR_ALLOWED_DIRS", "")
        if allowed_dirs_str:
            allowed_dirs = [d.strip() for d in allowed_dirs_str.split(":") if d.strip()]
        else:
            allowed_dirs = validated.filesystem.allowed_working_dirs

        # Tool whitelist from YAML (empty = allow all)
        allowed_tools = validated.tools.allowed

        # Denied paths from YAML only
        denied_paths = validated.filesystem.denied_paths

        # Denied tool args from YAML only
        denied_tool_args = _compile_denied_tool_args(validated.tools.denied_args)

        # Passthrough env from YAML
        passthrough_env = validated.executor.passthrough_env

        # Injected env from YAML (resolve env var refs)
        injected_env = {}
        for k, v in validated.executor.injected_env.items():
            # Resolve ${VAR} syntax if it matches exactly
            if v.startswith("${") and v.endswith("}"):
                env_var_name = v[2:-1]
                injected_env[k] = os.environ.get(env_var_name, "")
            else:
                injected_env[k] = v

        return cls(
            council_public_key_path=os.environ.get(
                "EXECUTOR_PUBLIC_KEY_PATH",
                validated.council.public_key_path,
            ),
            council_url=os.environ.get(
                "EXECUTOR_COUNCIL_URL",
                validated.council.url,
            ),
            redis_url=os.environ.get(
                "EXECUTOR_REDIS_URL",
                validated.redis.url,
            ),
            redis_key_prefix=validated.redis.key_prefix,
            redis_mode=os.environ.get("EXECUTOR_REDIS_MODE", validated.redis.mode),
            redis_sentinel_service_name=os.environ.get(
                "EXECUTOR_REDIS_SENTINEL_SERVICE_NAME",
                validated.redis.sentinel_service_name,
            ),
            redis_sentinel_nodes=_split_csv(
                os.environ.get(
                    "EXECUTOR_REDIS_SENTINEL_NODES",
                    ",".join(validated.redis.sentinel_nodes),
                )
            ),
            redis_sentinel_password=os.environ.get(
                "EXECUTOR_REDIS_SENTINEL_PASSWORD",
                validated.redis.sentinel_password,
            ),
            redis_connect_timeout_ms=int(
                os.environ.get(
                    "EXECUTOR_REDIS_CONNECT_TIMEOUT_MS",
                    str(validated.redis.connect_timeout_ms),
                )
            ),
            redis_command_timeout_ms=int(
                os.environ.get(
                    "EXECUTOR_REDIS_COMMAND_TIMEOUT_MS",
                    str(validated.redis.command_timeout_ms),
                )
            ),
            listen_host=os.environ.get(
                "EXECUTOR_HOST",
                validated.executor.listen_host,
            ),
            listen_port=int(
                os.environ.get(
                    "EXECUTOR_PORT",
                    str(validated.executor.listen_port),
                )
            ),
            executor_private_key_path=os.environ.get(
                "EXECUTOR_PRIVATE_KEY_PATH",
                validated.executor.identity_private_key_path,
            ),
            executor_public_key_path=os.environ.get(
                "EXECUTOR_PUBLIC_KEY_EXPORT_PATH",
                validated.executor.identity_public_key_path,
            ),
            allowed_working_directories=allowed_dirs,
            max_command_timeout=validated.executor.max_command_timeout,
            log_level=os.environ.get(
                "EXECUTOR_LOG_LEVEL",
                validated.executor.log_level,
            ),
            blocked_patterns=patterns,
            allowed_tools=allowed_tools,
            denied_paths=denied_paths,
            denied_tool_args=denied_tool_args,
            passthrough_env=passthrough_env,
            injected_env=injected_env,
        )

    @classmethod
    def load(cls) -> "ExecutorConfig":
        """Load config: YAML if EXECUTOR_CONFIG is set, else env vars.

        Env-only mode (no EXECUTOR_CONFIG) has no security policy — no blocked
        patterns, no tool whitelist, no denied paths. To prevent silent
        misconfiguration, env-only mode requires an explicit opt-in via
        EXECUTOR_UNSAFE_NO_POLICY=1. Without it the executor refuses to start.
        """
        config_path = os.environ.get("EXECUTOR_CONFIG", "")
        if config_path:
            return cls.from_yaml(config_path)
        if os.environ.get("EXECUTOR_UNSAFE_NO_POLICY", "").strip() != "1":
            raise RuntimeError(
                "EXECUTOR_CONFIG is not set and EXECUTOR_UNSAFE_NO_POLICY=1 is not present.\n"
                "The executor refuses to start without a security policy.\n"
                "Either set EXECUTOR_CONFIG to point at an executor.yaml file, or set\n"
                "EXECUTOR_UNSAFE_NO_POLICY=1 to explicitly acknowledge the zero-policy fallback\n"
                "(only appropriate for local development / testing)."
            )
        return cls.from_env()


def _validate_unimplemented_fields(validated, config_path: str) -> None:
    """Fail fast when yaml sets fields that are defined but not enforced at runtime."""
    unsupported_overrides: list[str] = []

    # Executor-level advanced auth/rate-limit fields are currently schema-only.
    if validated.executor.require_valid_token is not True:
        unsupported_overrides.append(
            "executor.require_valid_token=false (executor always performs writ verification)"
        )
    if validated.executor.require_agent_signature is not False:
        unsupported_overrides.append("executor.require_agent_signature=true")
    if validated.executor.enforce_rate_limits is not True:
        unsupported_overrides.append("executor.enforce_rate_limits=false")
    if validated.executor.max_requests_per_minute != 60:
        unsupported_overrides.append(
            f"executor.max_requests_per_minute={validated.executor.max_requests_per_minute}"
        )
    if validated.executor.max_concurrent_executions != 10:
        unsupported_overrides.append(
            f"executor.max_concurrent_executions={validated.executor.max_concurrent_executions}"
        )
    if validated.executor.allow_interactive_sessions is not False:
        unsupported_overrides.append("executor.allow_interactive_sessions=true")

    # Auth section is also schema-only today.
    if validated.auth.allowed_issuers != ["nomos-council"]:
        unsupported_overrides.append("auth.allowed_issuers")
    if validated.auth.required_audience != "writ-executor":
        unsupported_overrides.append(f"auth.required_audience={validated.auth.required_audience!r}")
    if validated.auth.token_leeway_seconds != 30:
        unsupported_overrides.append(
            f"auth.token_leeway_seconds={validated.auth.token_leeway_seconds}"
        )

    # Telemetry section is schema-only today.
    if validated.telemetry.enabled is not True:
        unsupported_overrides.append("telemetry.enabled=false")
    if validated.telemetry.prometheus_port != 9090:
        unsupported_overrides.append(
            f"telemetry.prometheus_port={validated.telemetry.prometheus_port}"
        )
    if validated.telemetry.otlp_endpoint:
        unsupported_overrides.append(
            f"telemetry.otlp_endpoint={validated.telemetry.otlp_endpoint!r}"
        )

    if unsupported_overrides:
        rendered = "\n".join(f"  - {field}" for field in unsupported_overrides)
        raise ValueError(
            f"Invalid executor config at {config_path}:\n"
            "The following fields are configured but not enforced"
            " by the current executor runtime.\n"
            "To prevent a false sense of security, startup is"
            " blocked when these fields are overridden:\n"
            f"{rendered}\n"
            "Use runtime-enforced controls (filesystem/tools/"
            "security sections), or remove these overrides "
            "until support is implemented."
        )

    _LOGGER.info(
        "Executor config loaded with runtime-enforced controls only "
        "(filesystem/tools/security + writ verification pipeline)."
    )


def _compile_denied_tool_args(
    raw: dict[str, list[str]],
) -> dict[str, list[re.Pattern]]:
    """Compile per-tool denied argument patterns."""
    result: dict[str, list[re.Pattern]] = {}
    for tool, patterns in raw.items():
        compiled = []
        for p in patterns:
            try:
                compiled.append(re.compile(p))
            except re.error:
                logging.getLogger(__name__).warning(
                    "Skipping invalid denied_args pattern for %s: %s", tool, p
                )
        if compiled:
            result[tool] = compiled
    return result


def _split_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _compile_blocked_patterns(
    patterns: "str | list[str]" = "",
) -> list[re.Pattern]:
    """Compile blocked patterns into regex objects.

    Accepts either:
      - A list of pattern strings (from YAML config)
      - A pipe-separated string (from EXECUTOR_BLOCKED_PATTERNS env var)
    """
    if isinstance(patterns, str):
        raw = [p.strip() for p in patterns.split("|") if p.strip()] if patterns.strip() else []
    else:
        raw = list(patterns)

    compiled = []
    for pattern in raw:
        try:
            compiled.append(re.compile(pattern))
        except re.error:
            logging.getLogger(__name__).warning("Skipping invalid blocked pattern: %s", pattern)
    return compiled
