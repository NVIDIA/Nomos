"""
Shell command execution handler.

Executes approved commands as local subprocesses with timeout,
working directory, and environment controls.
"""

import logging
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ShellResult:
    """Result of a shell command execution."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    duration_seconds: float = 0.0
    command: str = ""
    working_directory: str = ""

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def to_dict(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "duration_seconds": self.duration_seconds,
            "command": self.command,
            "working_directory": self.working_directory,
            "succeeded": self.succeeded,
        }


@dataclass
class ShellConfig:
    """Configuration for shell execution."""

    default_timeout_seconds: int = 30
    max_timeout_seconds: int = 300
    max_output_bytes: int = 1_000_000  # 1MB
    allowed_env_vars: list[str] = field(
        default_factory=lambda: [
            "PATH",
            "HOME",
            "USER",
            "SHELL",
            "LANG",
            "LC_ALL",
            "TERM",
            "TMPDIR",
            "TZ",
        ]
    )
    blocked_env_vars: list[str] = field(
        default_factory=lambda: [
            "AWS_SECRET_ACCESS_KEY",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "NVIDIA_API_KEY",
            "GITHUB_TOKEN",
            "SSH_PRIVATE_KEY",
        ]
    )


async def execute_shell_command(
    command: str,
    working_directory: str = "",
    timeout_seconds: int = 30,
    env_vars: dict[str, str] | None = None,
    config: ShellConfig | None = None,
) -> ShellResult:
    """
    Execute a shell command as a subprocess.

    Args:
        command: The shell command to execute
        working_directory: Directory to run in (defaults to cwd)
        timeout_seconds: Max seconds before killing the process
        env_vars: Additional environment variables to set
        config: Shell execution configuration

    Returns:
        ShellResult with stdout, stderr, exit code, timing
    """
    cfg = config or ShellConfig()
    timeout = min(timeout_seconds, cfg.max_timeout_seconds)
    # Default to COUNCIL_WORKSPACE if set (e.g., /app/workspace in container)
    cwd = working_directory or os.environ.get("COUNCIL_WORKSPACE") or None

    if cwd and not os.path.isdir(cwd):
        return ShellResult(
            exit_code=-1,
            stdout="",
            stderr=f"Working directory does not exist: {cwd}",
            command=command,
            working_directory=working_directory,
        )

    # Build sanitized environment
    env = _build_env(cfg, env_vars)

    logger.info("Executing shell command: %s (cwd=%s, timeout=%ds)", command, cwd, timeout)

    import time

    t0 = time.monotonic()

    try:
        try:
            completed = subprocess.run(  # noqa: S602
                command,
                shell=True,
                executable=os.environ.get("SHELL") or "/bin/sh",
                capture_output=True,
                text=False,
                cwd=cwd,
                env=env,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - t0
            return ShellResult(
                exit_code=-1,
                stdout="",
                stderr=f"Command timed out after {timeout}s",
                timed_out=True,
                duration_seconds=elapsed,
                command=command,
                working_directory=working_directory,
            )

        elapsed = time.monotonic() - t0

        stdout = completed.stdout.decode("utf-8", errors="replace")[: cfg.max_output_bytes]
        stderr = completed.stderr.decode("utf-8", errors="replace")[: cfg.max_output_bytes]

        result = ShellResult(
            exit_code=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=elapsed,
            command=command,
            working_directory=working_directory,
        )

        logger.info(
            "Command finished: exit_code=%d, duration=%.1fs",
            result.exit_code,
            result.duration_seconds,
        )
        return result

    except Exception as e:
        elapsed = time.monotonic() - t0
        logger.error("Command execution failed: %s", e)
        return ShellResult(
            exit_code=-1,
            stdout="",
            stderr=f"Execution error: {e}",
            duration_seconds=elapsed,
            command=command,
            working_directory=working_directory,
        )


def _build_env(
    config: ShellConfig,
    extra_vars: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build a sanitized environment for subprocess execution."""
    env: dict[str, str] = {}

    # Copy allowed vars from current environment
    for key in config.allowed_env_vars:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val

    # Add extra vars, filtering blocked ones
    if extra_vars:
        for key, val in extra_vars.items():
            if key not in config.blocked_env_vars:
                env[key] = val

    return env
