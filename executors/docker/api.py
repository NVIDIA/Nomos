# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""HTTP adapter for the trusted executor contract.

Versioned contract endpoints:
  POST /v1/writs/execute
  GET  /v1/health
  GET  /.well-known/nomos/trusted-executor.json

Legacy compatibility endpoints:
  POST /execute
  GET  /health
"""

import hashlib
import json
import logging
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from executors.common import (
    ExecutorIdentity,
    ProviderExecutorIdentity,
    certify_execution_result,
)
from trusted_executor_contract import (
    AGENT_ID_HEADER,
    CONTRACT_VERSION,
    EXECUTE_ENDPOINT,
    HEALTH_ENDPOINT,
    LEGACY_EXECUTE_ENDPOINT,
    LEGACY_HEALTH_ENDPOINT,
    WELL_KNOWN_ENDPOINT,
    ContractDescriptor,
    ExecuteWritRequest,
    ExecutionContext,
    ExecutionResult,
    ExecutorCapabilities,
    ExecutorHealth,
)
from trusted_executor_contract import (
    validate_agent_binding as _contract_validate_agent_binding,
)

from .config import ExecutorConfig
from .enforcer import WritEnforcer

logger = logging.getLogger(__name__)

MAX_REQUEST_SIZE = 1_048_576  # 1MB


def _validate_agent_binding(writ_blob: str, caller_agent_id: str) -> dict | None:
    """Compatibility wrapper for older tests/imports."""
    result = _contract_validate_agent_binding(writ_blob, caller_agent_id)
    return result.model_dump() if result else None


class ExecutorHandler(BaseHTTPRequestHandler):
    """HTTP request handler for writ execution."""

    enforcer: WritEnforcer | None = None
    public_key_fingerprint: str = ""
    start_time: float = 0.0
    executor_id: str = "example-local-subprocess"
    executor_identity: ExecutorIdentity | ProviderExecutorIdentity | None = None
    capabilities: ExecutorCapabilities | None = None
    _loop = None

    def log_message(self, format, *args):
        logger.info(format, *args)

    def do_GET(self):
        if self.path in {HEALTH_ENDPOINT, LEGACY_HEALTH_ENDPOINT}:
            self._handle_health()
        elif self.path == WELL_KNOWN_ENDPOINT:
            self._handle_contract_descriptor()
        else:
            self._send_json(404, {"error": "not_found"})

    def do_POST(self):
        if self.path in {EXECUTE_ENDPOINT, LEGACY_EXECUTE_ENDPOINT}:
            self._handle_execute()
        else:
            self._send_json(404, {"error": "not_found"})

    def _handle_health(self):
        health = ExecutorHealth(
            status="ok",
            contract_version=CONTRACT_VERSION,
            executor_id=self.executor_id,
            key_fingerprint=self.public_key_fingerprint,
            uptime_seconds=round(time.time() - self.start_time, 1),
            capabilities=self.capabilities or _example_capabilities(),
        )
        self._send_json(200, health.model_dump())

    def _handle_contract_descriptor(self):
        descriptor = ContractDescriptor(capabilities=self.capabilities or _example_capabilities())
        self._send_json(200, descriptor.model_dump())

    def _handle_execute(self):
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > MAX_REQUEST_SIZE:
            self._send_json(413, {"error": "Request too large"})
            return
        if content_length == 0:
            self._send_json(400, {"error": "Empty request body"})
            return

        try:
            body = self.rfile.read(content_length)
            request = ExecuteWritRequest.model_validate_json(body)
        except ValueError as e:
            self._send_json(400, {"error": f"Invalid request: {e}"})
            return

        caller_agent_id = self.headers.get(AGENT_ID_HEADER, "").strip()
        binding_err = _contract_validate_agent_binding(request.writ, caller_agent_id)
        if binding_err:
            self._send_result(request, binding_err)
            return

        if request.dry_run:
            self._send_result(
                request,
                ExecutionResult(
                    executor_id=self.executor_id,
                    success=False,
                    error="dry_run is not supported by this executor",
                    verification_stage="unsupported",
                ),
            )
            return

        if self.enforcer is None:
            self._send_json(500, {"error": "Enforcer not initialized"})
            return

        loop = self.__class__._loop
        if loop is None:
            self._send_json(500, {"error": "Event loop not initialized"})
            return

        try:
            context = ExecutionContext(
                caller_agent_id=caller_agent_id,
                request_id=request.request_id,
                remote_addr=self.client_address[0] if self.client_address else "",
                headers={key: value for key, value in self.headers.items()},
            )
            result = loop.run_until_complete(self.enforcer.execute_writ(request.writ, context))
            self._send_result(request, result)
        except Exception as e:
            logger.exception("Writ execution failed")
            self._send_result(
                request,
                ExecutionResult(
                    executor_id=self.executor_id,
                    writ_id="",
                    success=False,
                    error=str(e),
                    verification_stage="internal_error",
                ),
            )

    def _send_result(self, request: ExecuteWritRequest, result: ExecutionResult):
        result = result.model_copy(update={"executor_id": self.executor_id})
        if self.executor_identity is not None:
            result = certify_execution_result(
                request.writ,
                result,
                self.executor_identity,
                request_id=request.request_id,
            )
        self._send_json(200, result.model_dump(mode="json"))

    def _send_json(self, status_code: int, data: dict):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def create_server(
    config: ExecutorConfig,
    enforcer: WritEnforcer,
    public_key: bytes,
    loop=None,
    executor_identity: ExecutorIdentity | ProviderExecutorIdentity | None = None,
    executor_id_prefix: str = "example-local-subprocess",
    capabilities: ExecutorCapabilities | None = None,
) -> HTTPServer:
    """
    Create and configure the HTTP server.

    Parameters
    ----------
    config : ExecutorConfig
        The configuration for the server.
    enforcer : WritEnforcer
        The writ enforcer instance to use.
    public_key : bytes
        The raw 32-byte Ed25519 public key.
    loop : Optional[asyncio.AbstractEventLoop], optional
        The event loop to use for async operations, by default None.

    Returns
    -------
    HTTPServer
        The configured HTTP server instance.
    """
    import os

    in_container = os.path.exists("/.dockerenv") or os.environ.get("EXECUTOR_CONTAINER_MODE") == "1"
    allowed = {"127.0.0.1", "localhost", "::1"}
    if in_container:
        allowed.add("0.0.0.0")  # noqa: S104
    if config.listen_host not in allowed:
        raise ValueError(
            f"Executor must bind to loopback address"
            f" (or 0.0.0.0 in container mode), got: {config.listen_host}"
        )

    fingerprint = hashlib.sha256(public_key).hexdigest()
    executor_id = build_executor_id(executor_id_prefix, public_key, executor_identity)

    ExecutorHandler.enforcer = enforcer
    ExecutorHandler.public_key_fingerprint = fingerprint
    ExecutorHandler.start_time = time.time()
    ExecutorHandler.executor_id = executor_id
    ExecutorHandler.executor_identity = executor_identity
    configured_capabilities = capabilities or _example_capabilities()
    if executor_identity is not None:
        configured_capabilities = configured_capabilities.model_copy(
            update={
                "metadata": {
                    **configured_capabilities.metadata,
                    "evidence_signing": "ed25519",
                    "executor_key_fingerprint": executor_identity.key_fingerprint,
                }
            }
        )
    ExecutorHandler.capabilities = configured_capabilities
    ExecutorHandler._loop = loop

    from nomos_runtime import (
        ServerTLSConfig,
        ThreadedTLSHTTPServer,
        listener_tls_mode,
        require_strict_listener,
        strict_transport_profile,
    )

    listener_tls = ServerTLSConfig.from_env("EXECUTOR_LISTENER")
    require_strict_listener(
        listener_tls,
        label="Executor listener",
        strict=strict_transport_profile(os.environ.get("NOMOS_DEPLOY_PROFILE")),
    )
    server = ThreadedTLSHTTPServer((config.listen_host, config.listen_port), ExecutorHandler)
    context = listener_tls.ssl_context()
    server.configure_tls(context)
    logger.info(
        "Writ executor listening on %s:%d (%s; key fingerprint: %s)",
        config.listen_host,
        config.listen_port,
        listener_tls_mode(listener_tls),
        fingerprint[:16],
    )
    return server


def build_executor_id(
    executor_id_prefix: str,
    council_public_key: bytes,
    executor_identity: ExecutorIdentity | ProviderExecutorIdentity | None = None,
) -> str:
    """Return the stable executor id used in responses and evidence payloads."""
    if executor_identity is not None:
        fingerprint = executor_identity.key_fingerprint
    else:
        fingerprint = hashlib.sha256(council_public_key).hexdigest()
    return f"{executor_id_prefix}:{fingerprint[:16]}"


def _example_capabilities() -> ExecutorCapabilities:
    return ExecutorCapabilities(
        execution_modes=["local-subprocess"],
        supports_packaged_files=True,
        supports_dry_run=False,
        supports_async_execution=False,
        metadata={"evidence_signing": "ed25519"},
    )
