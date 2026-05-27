"""Minimal OpenShell-compatible runtime server for local development."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import time
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from typing import ClassVar

logger = logging.getLogger(__name__)

MAX_REQUEST_SIZE = 2_097_152
_SHELL_META = re.compile(r"[|&;<>()$`\\\"\']|\*|\?|\[|\]|\{|\}|~|\n")


class OpenShellRuntimeHandler(BaseHTTPRequestHandler):
    """HTTP handler for the dev OpenShell runtime."""

    allowed_working_dirs: ClassVar[list[str]] = ["/workspace"]

    def log_message(self, format, *args):
        logger.info(format, *args)

    def do_GET(self):
        if self.path == "/healthz":
            self._send_json(200, {"ok": True, "allowed_working_dirs": self.allowed_working_dirs})
            return
        self._send_json(404, {"error": "not_found"})

    def do_POST(self):
        if self.path != "/v1/execute":
            self._send_json(404, {"error": "not_found"})
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            self._send_json(400, {"error": "Empty request body"})
            return
        if content_length > MAX_REQUEST_SIZE:
            self._send_json(413, {"error": "Request too large"})
            return

        try:
            payload = json.loads(self.rfile.read(content_length))
        except json.JSONDecodeError as e:
            self._send_json(400, {"error": f"Invalid JSON: {e}"})
            return

        self._send_json(200, execute_payload(payload, self.allowed_working_dirs))

    def _send_json(self, status_code: int, data: dict):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class OpenShellRuntimeServer(ThreadingHTTPServer):
    """Threaded runtime server so parallel executor requests do not reset clients."""

    daemon_threads = True
    request_queue_size = 128


def execute_payload(payload: dict, allowed_working_dirs: list[str]) -> dict:
    """Execute one OpenShell runtime payload."""
    command = str(payload.get("command", ""))
    working_directory = str(payload.get("working_directory", ""))
    timeout_seconds = int(payload.get("timeout_seconds", 30))
    packaged_files = payload.get("packaged_files", [])

    if not command.strip():
        return _error("Command is empty")
    if "\x00" in command:
        return _error("Command contains null byte")
    if not _is_allowed_directory(working_directory, allowed_working_dirs):
        return _error(f"Working directory not allowed: {working_directory}")
    if not os.path.isdir(working_directory):
        return _error(f"Working directory does not exist: {working_directory}")

    attachment_error = _materialize_packaged_files(working_directory, packaged_files)
    if attachment_error:
        return _error(attachment_error)

    use_shell = _needs_shell(command)
    if use_shell:
        cmd = command
        argv: list[str] = []
    else:
        try:
            argv = shlex.split(command)
            cmd = argv
        except ValueError as e:
            return _error(f"Command parse failed: {e}")

    start_time = time.time()
    try:
        runtime_env = _runtime_env(working_directory, allowed_working_dirs)
        os.makedirs(runtime_env["TMPDIR"], mode=0o700, exist_ok=True)

        result = subprocess.run(
            cmd,
            shell=use_shell,
            cwd=working_directory,
            env=runtime_env,
            capture_output=True,
            timeout=timeout_seconds,
            text=True,
        )
    except subprocess.TimeoutExpired as e:
        return {
            "exit_code": None,
            "stdout": e.stdout or "",
            "stderr": e.stderr or "",
            "error": f"Command timed out after {timeout_seconds}s",
            "duration_seconds": time.time() - start_time,
            "shell": use_shell,
            "argv": argv,
        }
    except OSError as e:
        return _error(
            f"Execution failed: {e}",
            # Response field assignment, not process execution.
            shell=use_shell,  # nosec B604
            argv=argv,
        )

    return {
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "error": None,
        "duration_seconds": time.time() - start_time,
        "shell": use_shell,
        "argv": argv,
    }


def create_server(
    host: str = "127.0.0.1",
    port: int = 8080,
    allowed_working_dirs: list[str] | None = None,
) -> HTTPServer:
    OpenShellRuntimeHandler.allowed_working_dirs = allowed_working_dirs or ["/workspace"]
    return OpenShellRuntimeServer((host, port), OpenShellRuntimeHandler)


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, os.environ.get("OPENSHELL_LOG_LEVEL", "INFO"), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    host = os.environ.get("OPENSHELL_HOST", "127.0.0.1")
    port = int(os.environ.get("OPENSHELL_PORT", "8080"))
    allowed_dirs = [
        item.strip()
        for item in os.environ.get("OPENSHELL_ALLOWED_WORKDIRS", "/workspace").split(":")
        if item.strip()
    ]
    server = create_server(host, port, allowed_dirs)
    logger.info("OpenShell runtime listening on %s:%d", host, port)
    server.serve_forever()


def _error(error: str, *, shell: bool | None = None, argv: list[str] | None = None) -> dict:
    return {
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "error": error,
        "duration_seconds": 0.0,
        "shell": shell,
        "argv": argv or [],
    }


def _is_allowed_directory(directory: str, allowed_working_dirs: list[str]) -> bool:
    if not directory:
        return False
    resolved = os.path.realpath(directory)
    for allowed in allowed_working_dirs:
        allowed_resolved = os.path.realpath(allowed)
        if resolved == allowed_resolved or resolved.startswith(allowed_resolved + os.sep):
            return True
    return False


def _materialize_packaged_files(cwd: str, packaged_files: list[dict]) -> str | None:
    root = os.path.realpath(cwd)
    for packaged in packaged_files:
        path = str(packaged.get("path", ""))
        if not path or "\x00" in path:
            return "Packaged file path is invalid"

        destination = os.path.realpath(os.path.join(root, path))
        if destination != root and not destination.startswith(root + os.sep):
            return f"Packaged file path escapes working directory: {path}"

        try:
            content = base64.b64decode(str(packaged.get("content_base64", "")), validate=True)
        except (binascii.Error, ValueError) as e:
            return f"Packaged file {path!r} failed base64 decode: {e}"

        digest = hashlib.sha256(content).hexdigest()
        expected = str(packaged.get("sha256", ""))
        if expected and digest != expected:
            return f"Packaged file {path!r} hash mismatch"

        mode = str(packaged.get("mode", "0600"))
        try:
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            with open(destination, "wb") as f:
                f.write(content)
            os.chmod(destination, int(mode, 8))
        except (OSError, ValueError) as e:
            return f"Failed to materialize packaged file {path!r}: {e}"
    return None


def _runtime_env(working_directory: str, allowed_working_dirs: list[str]) -> dict[str, str]:
    workspace_tmp = os.path.join(working_directory, ".tmp")
    return {
        "HOME": _scoped_env_path("HOME", working_directory, allowed_working_dirs),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "TMPDIR": _scoped_env_path("TMPDIR", workspace_tmp, allowed_working_dirs),
        "PYTHONDONTWRITEBYTECODE": "1",
        "OPENSHELL_RUNTIME": "1",
    }


def _scoped_env_path(name: str, default: str, allowed_working_dirs: list[str]) -> str:
    value = os.environ.get(name, default)
    if _is_allowed_directory(value, allowed_working_dirs):
        return value
    return default


def _needs_shell(command: str) -> bool:
    return bool(_SHELL_META.search(command))


if __name__ == "__main__":
    main()
