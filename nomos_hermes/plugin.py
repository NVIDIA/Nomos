# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Hermes authorization gate and privacy-bounded live review context."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from nomos_client import AuthorizeRequest, ConsumeRequest
from nomos_client.client import AuthorizationClient
from nomos_client.transports.mcp import MCPAuthorizationClient
from trusted_executor_contract import AuthorizationExpectation, verify_authorization_writ

from .config import HermesNomosConfig
from .operations import (
    UNSUPPORTED_LOCAL_MUTATION_TOOLS,
    UnsupportedOperationError,
    extract_terminal_operation,
)
from .pending import PendingAuthorization, PendingAuthorizations
from .review_context import ReviewContextStore, build_review_context

logger = logging.getLogger("nomos.hermes")
_BLOCK_PREFIX = "BLOCKED by Nomos: "
SUPPORTED_HERMES_VERSION = "0.21.3"
HERMES_MAX_CALLBACK_TIMEOUT_SECONDS = 600.0


def _block(reason: str) -> dict[str, str]:
    return {"action": "block", "message": _BLOCK_PREFIX + reason[:300]}


def _log_field(value: Any, limit: int = 128) -> str:
    """Bound one untrusted correlation value to a single safe log token."""

    text = str(value or "")[:limit]
    return "".join(ch if ch.isalnum() or ch in "-._:" else "_" for ch in text)


class HermesAuthorizationGate:
    """Fail-closed adapter for Hermes Agent 0.21.3's native hook contract."""

    def __init__(
        self,
        config: HermesNomosConfig,
        client: AuthorizationClient,
        *,
        pending: PendingAuthorizations | None = None,
        review_contexts: ReviewContextStore | None = None,
        hook_safety_check: Callable[[Callable[..., Any], float], None] | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._pending = pending or PendingAuthorizations()
        self._review_contexts = review_contexts or ReviewContextStore()
        self._hook_safety_check = hook_safety_check or _verify_hermes_hook_safety
        # Preserve one bound-method identity so the runtime can distinguish this
        # callback from competing pre-tool callbacks.
        self.pre_callback = self.pre_tool_call

    def pre_api_request(
        self,
        request_messages: Any = None,
        user_message: Any = None,
        task_id: str = "",
        session_id: str = "",
        turn_id: str = "",
        api_request_id: str = "",
        retry_count: int = 0,
        **_: Any,
    ) -> None:
        """Capture optional live-review evidence without retaining hook input.

        Hermes observers are fail-open. Capture failure therefore leaves no
        snapshot; the later authorization still runs with empty context rather
        than treating absence as either safe evidence or an execution failure.
        """

        if not all((session_id, task_id, turn_id, api_request_id)):
            return
        try:
            normalized_retry = max(0, int(retry_count or 0))
        except (TypeError, ValueError):
            normalized_retry = 0
        key = (session_id, task_id, turn_id, api_request_id)
        try:
            snapshot = build_review_context(
                request_messages,
                current_user_message=user_message,
                retry_count=normalized_retry,
            )
            if snapshot is not None:
                self._review_contexts.put(key, snapshot)
            else:
                self._review_contexts.mark_unavailable(key, retry_count=normalized_retry)
        except BaseException:
            self._review_contexts.mark_unavailable(key, retry_count=normalized_retry)
            # Never interpolate the exception: malformed provider objects can
            # include request text or credentials in their representation.
            logger.warning("Nomos review context capture unavailable reason=capture_error")

    def on_session_end(self, session_id: str = "", **_: Any) -> None:
        """Release per-turn context promptly; monotonic TTL remains the backstop."""

        if session_id:
            self._review_contexts.delete_session(session_id)

    def pre_tool_call(
        self,
        tool_name: str,
        args: dict[str, Any],
        task_id: str = "",
        session_id: str = "",
        tool_call_id: str = "",
        turn_id: str = "",
        api_request_id: str = "",
        **_: Any,
    ) -> dict[str, str] | None:
        """Synchronously gate one call in Hermes's outer bounded worker.

        Hermes 0.21.3 applies a separate hard-coded 30-second resolver to
        awaitable hook results. Returning an awaitable would therefore let an
        ordinary resolver TimeoutError be logged and skipped. This synchronous
        wrapper owns the event loop and bounds the complete authorize, verify,
        and consume sequence below Hermes's fail-closed callback deadline.
        """

        try:

            async def run_bounded() -> dict[str, str] | None:
                return await asyncio.wait_for(
                    self._authorize_tool_call(
                        tool_name,
                        args,
                        task_id=task_id,
                        session_id=session_id,
                        tool_call_id=tool_call_id,
                        turn_id=turn_id,
                        api_request_id=api_request_id,
                    ),
                    timeout=self._config.timeout_seconds,
                )

            return asyncio.run(run_bounded())
        except UnsupportedOperationError as exc:
            return _block(str(exc))
        except BaseException as exc:
            # Hermes logs and skips ordinary hook exceptions. Converting every
            # failure into a directive is therefore required for fail-closed
            # behavior. Do not log exception text: transports may embed secrets.
            logger.error("Nomos authorization failed safely (%s)", type(exc).__name__)
            return _block("authorization validation failed")

    async def _authorize_tool_call(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        task_id: str,
        session_id: str,
        tool_call_id: str,
        turn_id: str,
        api_request_id: str,
    ) -> dict[str, str] | None:
        operation = extract_terminal_operation(tool_name, args)
        if operation is None:
            return None
        if not session_id or not tool_call_id:
            return _block("Hermes did not provide required correlation identifiers")
        self._hook_safety_check(self.pre_callback, self._config.timeout_seconds)
        review_context = self._review_contexts.get(
            (session_id, task_id, turn_id, api_request_id)
        ) if all((session_id, task_id, turn_id, api_request_id)) else None
        decision = await self._client.authorize(
            AuthorizeRequest(
                auth_token=self._config.auth_token,
                agent_id=self._config.agent_id,
                council_id=self._config.council_id,
                command=operation.command,
                working_directory=operation.working_directory,
                target_resource=operation.working_directory,
                # Correlation identifiers remain adapter metadata. Only the
                # bounded message projection is live review evidence, and its
                # absence never implies that the requested operation is safe.
                context=review_context.serialized if review_context else "",
            )
        )
        if decision.status != "approved" or not decision.writ:
            return _block("Council denied the terminal operation")
        verified = verify_authorization_writ(
            decision.writ,
            council_public_keys=self._config.council_public_keys,
            expectation=AuthorizationExpectation(
                council_id=self._config.council_id,
                agent_id=self._config.agent_id,
                command=operation.command,
                working_directory=operation.working_directory,
                max_lifetime_seconds=self._config.max_writ_lifetime_seconds,
                clock_skew_seconds=self._config.clock_skew_seconds,
            ),
        )
        if decision.writ_id and decision.writ_id != verified.writ.writ_id:
            return _block("Council response and signed writ do not match")
        consumed = await self._client.consume(
            ConsumeRequest(
                council_id=self._config.council_id,
                writ_id=verified.writ.writ_id,
                nonce=verified.writ.nonce,
                auth_token=self._config.auth_token,
            )
        )
        if (
            not consumed.consumed
            or consumed.status != "consumed"
            or consumed.writ_id != verified.writ.writ_id
            or consumed.council_id != self._config.council_id
        ):
            return _block("The signed authorization could not be consumed")
        self._pending.put(
            (session_id, tool_call_id),
            PendingAuthorization(
                writ_id=verified.writ.writ_id,
                decision_id=decision.decision_id,
                command=operation.command,
                working_directory=operation.working_directory,
                created_at=time.time(),
            ),
        )
        return None

    def post_tool_call(
        self,
        tool_name: str,
        args: dict[str, Any],
        session_id: str = "",
        tool_call_id: str = "",
        status: str = "",
        duration_ms: int = 0,
        error_type: str = "",
        **_: Any,
    ) -> None:
        """Record bounded Hermes-observed completion metadata, not execution proof."""

        record = self._pending.pop((session_id, tool_call_id))
        if record is None:
            return
        exact_args = (
            tool_name == "terminal"
            and args.get("command") == record.command
            and args.get("workdir") == record.working_directory
        )
        logger.info(
            "Nomos observed Hermes tool completion writ_id=%s decision_id=%s "
            "status=%s duration_ms=%d error_type=%s "
            "arguments_matched_authorization=%s",
            _log_field(record.writ_id),
            _log_field(record.decision_id),
            _log_field(status or "unknown", 32),
            max(0, int(duration_ms or 0)),
            _log_field(error_type, 80),
            exact_args,
        )


def _verify_hermes_hook_safety(callback: Callable[..., Any], timeout_seconds: float) -> None:
    """Reject configurations that can mutate arguments after authorization."""

    from hermes_cli.config import load_config_readonly
    from hermes_cli.plugins import iter_hook_callbacks

    callbacks = iter_hook_callbacks("pre_tool_call")
    if len(callbacks) != 1 or callbacks[0] is not callback:
        raise RuntimeError("Nomos must be the sole registered Python pre_tool_call callback")
    config = load_config_readonly() or {}
    plugins = config.get("plugins") if isinstance(config, dict) else None
    hook_timeout = plugins.get("hook_callback_timeout", 30) if isinstance(plugins, dict) else 30
    configured_hook_timeout = float(hook_timeout)
    if not math.isfinite(configured_hook_timeout) or configured_hook_timeout <= 0:
        raise RuntimeError("Hermes hook callback timeout must be finite and positive")
    effective_hook_timeout = min(configured_hook_timeout, HERMES_MAX_CALLBACK_TIMEOUT_SECONDS)
    if effective_hook_timeout <= timeout_seconds:
        raise RuntimeError("Hermes hook callback timeout must exceed the Nomos client timeout")
    hooks = config.get("hooks") if isinstance(config, dict) else None
    if isinstance(hooks, dict) and hooks.get("pre_tool_call"):
        raise RuntimeError("shell pre_tool_call hooks are incompatible with exact writ binding")


def _build_gate() -> HermesAuthorizationGate | None:
    config = HermesNomosConfig.from_env()
    if not config.enabled:
        return None
    try:
        installed_version = version("hermes-agent")
    except PackageNotFoundError as exc:
        raise ValueError("Hermes Agent distribution is not installed") from exc
    if installed_version != SUPPORTED_HERMES_VERSION:
        raise ValueError(
            f"Hermes Agent {installed_version} is unsupported; expected {SUPPORTED_HERMES_VERSION}"
        )
    headers = (
        {"Authorization": f"Bearer {config.transport_token}"} if config.transport_token else None
    )
    client = MCPAuthorizationClient(
        config.mcp_url,
        headers=headers,
        timeout_seconds=config.timeout_seconds,
    )
    return HermesAuthorizationGate(config, client)


def register(ctx: Any) -> None:
    """Hermes plugin entry point.

    Invalid enabled configuration still registers a blocker so an importable
    but misconfigured plugin cannot silently allow local execution. Deployment
    readiness must additionally prove that this entry point was discovered.
    """

    try:
        gate = _build_gate()
    except BaseException as exc:
        logger.error("Nomos Hermes plugin configuration is invalid (%s)", type(exc).__name__)

        def block_misconfigured(tool_name: str, **_: Any) -> dict[str, str] | None:
            if tool_name == "terminal" or tool_name in UNSUPPORTED_LOCAL_MUTATION_TOOLS:
                return _block("plugin configuration is invalid")
            return None

        ctx.register_hook("pre_tool_call", block_misconfigured)
        return
    if gate is None:
        return
    ctx.register_hook("pre_tool_call", gate.pre_callback)
    ctx.register_hook("post_tool_call", gate.post_tool_call)
    ctx.register_hook("pre_api_request", gate.pre_api_request)
    ctx.register_hook("on_session_end", gate.on_session_end)
