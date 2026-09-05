# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Small HTTP client for an OpenShell execution backend."""

import asyncio
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

from nomos_runtime import ClientTLSConfig, require_secure_url, strict_transport_profile
from trusted_executor_contract import PackagedFile


@dataclass
class OpenShellResult:
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    shell: bool | None = None
    argv: list[str] | None = None


class OpenShellClient:
    """Posts approved commands to an OpenShell-compatible HTTP backend."""

    def __init__(self, endpoint: str, request_timeout: int = 30):
        self._endpoint = endpoint.rstrip("/")
        self._request_timeout = request_timeout
        self._tls = ClientTLSConfig.from_env("OPENSHELL")
        require_secure_url(
            self._endpoint,
            tls=self._tls,
            label="OPENSHELL_ENDPOINT",
            strict=strict_transport_profile(os.environ.get("NOMOS_DEPLOY_PROFILE")),
        )

    async def execute(
        self,
        *,
        command: str,
        working_directory: str,
        timeout_seconds: int,
        packaged_files: list[PackagedFile],
        metadata: dict[str, str],
    ) -> OpenShellResult:
        if not self._endpoint:
            return OpenShellResult(error="OpenShell endpoint is not configured")
        return await asyncio.to_thread(
            self._execute_sync,
            command,
            working_directory,
            timeout_seconds,
            packaged_files,
            metadata,
        )

    def _execute_sync(
        self,
        command: str,
        working_directory: str,
        timeout_seconds: int,
        packaged_files: list[PackagedFile],
        metadata: dict[str, str],
    ) -> OpenShellResult:
        payload = {
            "command": command,
            "working_directory": working_directory,
            "timeout_seconds": timeout_seconds,
            "packaged_files": [item.content_for_hashing() for item in packaged_files],
            "metadata": metadata,
        }
        req = urllib.request.Request(  # noqa: S310
            f"{self._endpoint}/v1/execute",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(  # type: ignore[call-overload]  # noqa: S310
                req,
                timeout=min(timeout_seconds, self._request_timeout),
                **self._tls.urllib_kwargs(),
            ) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return OpenShellResult(error=f"OpenShell HTTP {e.code}: {e.reason}")
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            return OpenShellResult(error=f"OpenShell request failed: {e}")

        return OpenShellResult(
            exit_code=body.get("exit_code"),
            stdout=body.get("stdout", ""),
            stderr=body.get("stderr", ""),
            error=body.get("error"),
            # Response field assignment, not process execution.
            shell=body.get("shell"),  # nosec B604
            argv=body.get("argv") or [],
        )
