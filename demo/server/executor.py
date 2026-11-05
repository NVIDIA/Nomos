# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Thin async wrapper around the production executor HTTP API.

Two contract endpoints are used:
  - POST /v1/writs/execute  — submit a signed writ, get stdout/stderr/exit_code
  - GET  /v1/health         — key fingerprint, uptime, capabilities
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from nomos_runtime import ClientTLSConfig, require_secure_url, strict_transport_profile

logger = logging.getLogger(__name__)


class ExecutorClient:
    def __init__(self, executor_url: str, timeout: float = 60.0):
        self.executor_url = executor_url.rstrip("/")
        self.timeout = timeout
        self.tls = ClientTLSConfig.from_env("NOMOS_EXECUTOR")
        require_secure_url(
            self.executor_url,
            tls=self.tls,
            label="NOMOS_EXECUTOR_URL",
            strict=strict_transport_profile(os.environ.get("NOMOS_DEPLOY_PROFILE")),
        )

    async def execute(self, writ_blob: str, agent_id: str) -> dict[str, Any]:
        """POST a signed writ blob with the X-Agent-ID header."""
        headers = {
            "Content-Type": "application/json",
            "X-Agent-ID": agent_id,
        }
        async with httpx.AsyncClient(
            timeout=self.timeout,
            verify=self.tls.ssl_context(),
        ) as client:
            resp = await client.post(
                f"{self.executor_url}/v1/writs/execute",
                json={"writ": writ_blob},
                headers=headers,
            )
        # The executor returns 200 with {success: false, ...} for verification
        # failures, and 200 with {success: true, ...} on execution. Non-200
        # means the executor itself failed — surface that to the client.
        try:
            payload = resp.json()
            return (
                payload
                if isinstance(payload, dict)
                else {"success": False, "error": "invalid response"}
            )
        except ValueError:
            return {
                "success": False,
                "error": f"executor returned non-JSON (HTTP {resp.status_code})",
                "raw": resp.text[:500],
            }

    async def health(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=5.0, verify=self.tls.ssl_context()) as client:
            resp = await client.get(f"{self.executor_url}/v1/health")
        try:
            payload = resp.json()
        except ValueError:
            return {"status": "invalid response"}
        return payload if isinstance(payload, dict) else {"status": "invalid response"}
