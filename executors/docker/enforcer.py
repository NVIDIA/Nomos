"""
Writ Enforcer — core verification and execution logic.

Verification steps (ALL must pass, in order):
1. Deserialize: Base64-decode → JSON parse → WritOfExecution
2. Signature verification against council public key
3. Temporal check: issued_at <= now <= expires_at
4. Nonce check: atomically claim nonce in Redis
5. Working directory validation: must be in allowed list
6. Blocked pattern check: reject catastrophic commands
7. Tool whitelist check: binary must be in allowed_tools
8. Denied tool args check: per-tool argument restrictions
9. Denied paths check: command must not reference forbidden paths
10. Command non-empty check

If any check fails → return error, do NOT execute.
"""

import base64
import hashlib
import logging
import os
import re
import shlex
import subprocess
import time

from .config import ExecutorConfig
from .models import ExecutionContext, ExecutionResult, WritOfExecution
from .nonce_store import NonceStore

# Shell metacharacters that require shell=True to interpret correctly.
_SHELL_META = re.compile(r"[|&;<>()$`\\\"\']|\*|\?|\[|\]|\{|\}|~|\n")
_COMMAND_SEPARATORS = {";", "&&", "||", "|", "&", "\n"}
_COMMAND_GROUP_TOKENS = {"(", ")"}
_REDIRECT_TOKENS = {">", ">>", "<", "<<", "2>", "2>>", "&>", ">&"}

logger = logging.getLogger(__name__)


class WritEnforcer:
    """Verifies and executes Writs of Execution.

    Supports multiple public keys for key rotation. When a writ includes
    a key_id field, the enforcer looks up the corresponding key. If no
    key_id or only one key is configured, the default key is used.
    """

    def __init__(
        self,
        public_key: bytes,
        nonce_store: NonceStore,
        config: ExecutorConfig,
        additional_keys: dict | None = None,
    ):
        self._public_key = public_key
        self._key_fingerprint = hashlib.sha256(public_key).hexdigest()
        self._keys: dict = {self._key_fingerprint: public_key}
        if additional_keys:
            self._keys.update(additional_keys)
        self._nonce_store = nonce_store
        self._config = config

    def _resolve_public_key(self, writ: WritOfExecution) -> bytes | None:
        """
        Resolve the public key for signature verification.

        Checks writ's council_id or falls back to default key.
        For key rotation, additional_keys maps key_id → public_key_bytes.

        Parameters
        ----------
        writ : WritOfExecution
            The writ to verify.

        Returns
        -------
        Optional[bytes]
            The resolved public key bytes, or None to signal all keys should be tried.
        """
        # If only one key, use it
        if len(self._keys) == 1:
            return self._public_key
        # Try to match by fingerprint if stored in writ metadata
        # For now, try all keys (small set — typically 1-2 during rotation)
        return None  # Signal to try all keys

    def _verify_signature(self, writ: WritOfExecution) -> bool:
        """
        Verify signature, trying all known keys if needed.

        Parameters
        ----------
        writ : WritOfExecution
            The writ to verify.

        Returns
        -------
        bool
            True if the signature is valid against any known key, False otherwise.
        """
        resolved = self._resolve_public_key(writ)
        if resolved is not None:
            return writ.verify(resolved)
        # Try all keys
        for key_bytes in self._keys.values():
            if writ.verify(key_bytes):
                return True
        return False

    async def execute_writ(
        self,
        serialized_writ: str,
        context: ExecutionContext | None = None,
    ) -> ExecutionResult:
        """
        Verify and execute a serialized writ.

        Parameters
        ----------
        serialized_writ : str
            The base64-encoded, JSON-serialized writ.
        context : Optional[ExecutionContext], optional
            Caller metadata supplied by the HTTP contract adapter. The
            example enforcer does not currently need it because agent
            binding is handled before this method is called.

        Returns
        -------
        ExecutionResult
            The result of verification and execution. verification_stage
            indicates which check failed (or "passed" if execution completed).
        """
        # 1. Deserialize
        try:
            json_bytes = base64.b64decode(serialized_writ)
            writ = WritOfExecution.model_validate_json(json_bytes)
        except Exception as e:
            return ExecutionResult(
                error=f"Deserialization failed: {e}",
                verification_stage="deserialize",
            )

        # 2. Signature verification
        if not self._verify_signature(writ):
            return ExecutionResult(
                writ_id=writ.writ_id,
                error="Signature verification failed",
                verification_stage="signature",
            )

        # 3. Temporal check
        now = time.time()
        if now < writ.issued_at:
            return ExecutionResult(
                writ_id=writ.writ_id,
                error=f"Writ not yet valid (issued_at={writ.issued_at}, now={now})",
                verification_stage="temporal",
            )
        if writ.is_expired(now):
            return ExecutionResult(
                writ_id=writ.writ_id,
                error=f"Writ expired at {writ.expires_at} (now={now})",
                verification_stage="temporal",
            )

        # 4. Nonce check (atomic claim)
        nonce_ttl = int(writ.expires_at - now) + 86400  # 24h safety margin
        nonce_claimed = await self._nonce_store.claim_nonce(
            writ.nonce,
            writ.writ_id,
            nonce_ttl,
        )
        if not nonce_claimed:
            return ExecutionResult(
                writ_id=writ.writ_id,
                error="Nonce already claimed (replay detected)",
                verification_stage="nonce",
            )

        # From here, nonce is permanently claimed — never released.
        # A writ that reaches this point is consumed regardless of outcome.
        # If local policy rejects it, the agent must obtain a new writ.

        # 5. Working directory validation
        if not self._is_allowed_directory(writ.working_directory):
            reason = (
                "Working directory is empty — an explicit cwd is required"
                if not writ.working_directory
                else f"Working directory not allowed: {writ.working_directory}"
            )
            return ExecutionResult(
                writ_id=writ.writ_id,
                error=reason,
                verification_stage="directory",
            )

        # 6. Blocked pattern check
        blocked = self._check_blocked_patterns(writ.command)
        if blocked:
            return ExecutionResult(
                writ_id=writ.writ_id,
                error=f"Command blocked by safety pattern: {blocked}",
                verification_stage="blocked_pattern",
            )

        # 7. Tool whitelist check
        tool_err = self._check_tool_whitelist(writ.command)
        if tool_err:
            return ExecutionResult(
                writ_id=writ.writ_id,
                error=tool_err,
                verification_stage="tool_whitelist",
            )

        # 8. Denied tool args check
        args_err = self._check_denied_tool_args(writ.command)
        if args_err:
            return ExecutionResult(
                writ_id=writ.writ_id,
                error=args_err,
                verification_stage="denied_args",
            )

        # 9. Denied paths check
        path_err = self._check_denied_paths(writ.command)
        if path_err:
            return ExecutionResult(
                writ_id=writ.writ_id,
                error=path_err,
                verification_stage="denied_path",
            )

        # 10. Command non-empty check
        if not writ.command or not writ.command.strip():
            return ExecutionResult(
                writ_id=writ.writ_id,
                error="Command is empty",
                verification_stage="command",
            )

        attachment_err = self._unpack_packaged_files(writ)
        if attachment_err:
            return attachment_err

        # All checks passed — execute
        try:
            return await self._execute_command(writ)
        except Exception as e:
            return ExecutionResult(
                writ_id=writ.writ_id,
                error=f"Execution error: {e}",
                verification_stage="execution",
            )

    def _check_blocked_patterns(self, command: str) -> str | None:
        """
        Check command against blocked patterns.

        Parameters
        ----------
        command : str
            The command to check.

        Returns
        -------
        Optional[str]
            The matched pattern string if blocked, None otherwise.
        """
        for pattern in self._config.blocked_patterns:
            if pattern.search(command):
                logger.warning("Command blocked by pattern %r: %s", pattern.pattern, command[:120])
                return pattern.pattern
        return None

    def _extract_tool_name(self, command: str) -> str:
        """
        Extract the first token (binary name) from a shell command.

        Parameters
        ----------
        command : str
            The shell command to extract from.

        Returns
        -------
        str
            The binary name or empty string if not found.
        """
        tools = self._extract_tool_names(command)
        return tools[0] if tools else ""

    @staticmethod
    def _is_env_assignment(token: str) -> bool:
        """Return True for shell-style NAME=value assignments before a command."""
        name, sep, _ = token.partition("=")
        return bool(sep and name and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name))

    @staticmethod
    def _shell_tokens(command: str) -> list[str]:
        """Tokenize a shell command while preserving command separators."""
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;()<>")
            lexer.whitespace_split = True
            return list(lexer)
        except ValueError:
            return command.strip().split()

    def _extract_tool_names(self, command: str) -> list[str]:
        """
        Extract command words from a shell command.

        This scans simple shell control operators so local policy applies to
        every stage of a compound command, not just the first token.
        """
        tokens = self._shell_tokens(command)
        tools: list[str] = []
        expect_command = True
        skip_redirect_target = False

        for word in tokens:
            if not word:
                continue

            if word in _COMMAND_SEPARATORS:
                expect_command = True
                skip_redirect_target = False
                continue

            if word in _COMMAND_GROUP_TOKENS:
                if word == "(":
                    expect_command = True
                continue

            if word in _REDIRECT_TOKENS or set(word) <= {">", "<"}:
                skip_redirect_target = True
                continue

            if skip_redirect_target:
                skip_redirect_target = False
                continue

            if expect_command:
                if self._is_env_assignment(word):
                    continue
                tools.append(os.path.basename(word))
                expect_command = False

        return tools

    def _unpack_packaged_files(self, writ: WritOfExecution) -> ExecutionResult | None:
        """Materialize signed packaged files under the writ working directory."""
        if not writ.packaged_files:
            return None

        cwd = os.path.realpath(writ.working_directory)
        for packaged_file in writ.packaged_files:
            try:
                content = packaged_file.decoded_bytes()
            except ValueError as e:
                return ExecutionResult(
                    writ_id=writ.writ_id,
                    error=f"Packaged file {packaged_file.path!r} failed base64 decode: {e}",
                    verification_stage="attachment",
                )

            digest = hashlib.sha256(content).hexdigest()
            if digest != packaged_file.sha256:
                return ExecutionResult(
                    writ_id=writ.writ_id,
                    error=f"Packaged file {packaged_file.path!r} hash mismatch",
                    verification_stage="attachment",
                )

            destination = os.path.realpath(os.path.join(cwd, packaged_file.path))
            if destination != cwd and not destination.startswith(cwd + os.sep):
                return ExecutionResult(
                    writ_id=writ.writ_id,
                    error=f"Packaged file path escapes working directory: {packaged_file.path}",
                    verification_stage="attachment",
                )

            try:
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                with open(destination, "wb") as f:
                    f.write(content)
                os.chmod(destination, int(packaged_file.mode, 8))
            except OSError as e:
                return ExecutionResult(
                    writ_id=writ.writ_id,
                    error=f"Failed to unpack packaged file {packaged_file.path!r}: {e}",
                    verification_stage="attachment",
                )

        return None

    def _check_tool_whitelist(self, command: str) -> str | None:
        """
        Check if the command's binary is in the allowed tools list.

        Empty allowed_tools list means allow-all (disabled).

        Parameters
        ----------
        command : str
            The shell command to check.

        Returns
        -------
        Optional[str]
            Error string if blocked, None if allowed.
        """
        if not self._config.allowed_tools:
            return None
        tools = self._extract_tool_names(command)
        if not tools:
            return None
        disallowed = [tool for tool in tools if tool not in self._config.allowed_tools]
        if disallowed:
            logger.warning("Tools not in whitelist: %s", ", ".join(disallowed))
            return f"Tool not allowed: {disallowed[0]}"
        return None

    def _check_denied_tool_args(self, command: str) -> str | None:
        """Check per-tool denied argument patterns.
        Returns error string if blocked, None if allowed."""
        if not self._config.denied_tool_args:
            return None
        for tool in self._extract_tool_names(command):
            if tool not in self._config.denied_tool_args:
                continue
            for pattern in self._config.denied_tool_args[tool]:
                if pattern.search(command):
                    logger.warning(
                        "Denied args for %s matched %r: %s",
                        tool,
                        pattern.pattern,
                        command[:120],
                    )
                    return f"Blocked argument for {tool}: {pattern.pattern}"
        return None

    def _check_denied_paths(self, command: str) -> str | None:
        """Check if the command references any denied paths.

        Performs two checks:
        1. Substring match on the raw command (catches obvious references)
        2. Resolves path-like tokens via os.path.realpath() and checks
           resolved paths against denied list (catches symlinks, relative
           paths, and ./ prefixes)

        Returns error string if blocked, None if allowed.
        """
        if not self._config.denied_paths:
            return None

        # 1. Substring match on raw command
        for denied in self._config.denied_paths:
            if denied in command:
                logger.warning(
                    "Command references denied path %s: %s",
                    denied,
                    command[:120],
                )
                return f"Path denied: {denied}"

        # 2. Resolve path-like tokens and re-check
        for token in self._extract_path_tokens(command):
            resolved = os.path.realpath(token)
            for denied in self._config.denied_paths:
                if denied in resolved:
                    logger.warning(
                        "Resolved path %s (from %s) matches denied path %s",
                        resolved,
                        token,
                        denied,
                    )
                    return f"Path denied: {denied} (resolved from {token})"
        return None

    @staticmethod
    def _extract_path_tokens(command: str) -> list[str]:
        """Extract tokens that look like filesystem paths from a command."""
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = command.strip().split()
        paths = []
        for token in tokens:
            # Skip flags, env assignments, and very short tokens
            if token.startswith("-") or "=" in token or len(token) < 2:
                continue
            # Looks like a path: starts with /, ./, ../, ~, or contains /
            if token.startswith(("/", "./", "../", "~")) or "/" in token:
                paths.append(token)
        return paths

    @staticmethod
    def _needs_shell(command: str) -> bool:
        """Return True if the command requires shell interpretation.

        Simple commands (e.g. ``echo hello``, ``cat /tmp/file.txt``) can
        run with ``shell=False`` + ``shlex.split()``, which prevents
        injection of shell metacharacters.  Commands that contain pipes,
        redirects, subshells, variable expansion, globs, etc. still need
        ``shell=True``.
        """
        return bool(_SHELL_META.search(command))

    def _is_allowed_directory(self, directory: str) -> bool:
        """Check if a directory is in the allowed list.

        Requires an explicit, non-empty cwd. An empty value is denied
        because subprocess.run(cwd=None) would inherit the executor's
        own WORKDIR (/app per deploy/Dockerfile.executor), exposing
        its config and the council public key dir.
        """
        if not directory:
            return False

        resolved = os.path.realpath(directory)
        for allowed in self._config.allowed_working_directories:
            allowed_resolved = os.path.realpath(allowed)
            if resolved == allowed_resolved or resolved.startswith(allowed_resolved + os.sep):
                return True
        return False

    async def _execute_command(self, writ: WritOfExecution) -> ExecutionResult:
        """Execute the command in a subprocess."""
        logger.debug("Executing writ %s", writ.writ_id)

        # Base environment starts empty
        env = {}

        # Pass through configured env vars from the executor process
        for var in self._config.passthrough_env:
            if var in os.environ:
                env[var] = os.environ[var]

        # Inject configured environment variables (including resolved secrets)
        for k, v in self._config.injected_env.items():
            env[k] = v

        # Ensure minimal PATH if not provided
        if "PATH" not in env:
            env["PATH"] = "/usr/local/bin:/usr/bin:/bin"

        cwd = writ.working_directory or None
        timeout = min(
            self._config.max_command_timeout,
            int(writ.expires_at - time.time()),
        )
        if timeout <= 0:
            return ExecutionResult(
                writ_id=writ.writ_id,
                error="Writ expired before execution could start",
                verification_stage="temporal",
            )

        if "\x00" in writ.command:
            return ExecutionResult(
                writ_id=writ.writ_id,
                error="Command contains null byte — rejected",
                verification_stage="blocked_pattern",
            )

        use_shell = self._needs_shell(writ.command)
        if use_shell:
            cmd = writ.command
        else:
            try:
                cmd = shlex.split(writ.command)
            except ValueError:
                # Malformed quoting — fall back to shell
                cmd = writ.command
                use_shell = True

        start_time = time.time()
        try:
            result = subprocess.run(
                cmd,
                shell=use_shell,
                cwd=cwd,
                env=env,
                capture_output=True,
                timeout=timeout,
                text=True,
            )
            duration = time.time() - start_time

            # Report outcome (best-effort)
            stdout_hash = hashlib.sha256(result.stdout.encode()).hexdigest()
            stderr_hash = hashlib.sha256(result.stderr.encode()).hexdigest()
            await self._nonce_store.report_outcome(
                writ_id=writ.writ_id,
                exit_code=result.returncode,
                stdout_hash=stdout_hash,
                stderr_hash=stderr_hash,
            )

            return ExecutionResult(
                writ_id=writ.writ_id,
                success=result.returncode == 0,
                exit_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                duration_seconds=duration,
                verification_stage="passed",
                executed_command_shell=use_shell,
                executed_command_argv=[] if use_shell else cmd,
            )

        except subprocess.TimeoutExpired as e:
            duration = time.time() - start_time
            return ExecutionResult(
                writ_id=writ.writ_id,
                success=False,
                stdout=e.stdout or "" if hasattr(e, "stdout") else "",
                stderr=e.stderr or "" if hasattr(e, "stderr") else "",
                duration_seconds=duration,
                error=f"Command timed out after {timeout}s",
                verification_stage="timeout",
                executed_command_shell=use_shell,
                executed_command_argv=[] if use_shell else cmd,
            )
