# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Resolve an exact structured call against one operator-owned council catalog."""

from __future__ import annotations

import asyncio
import contextvars
import time
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial

from nomos.level_0.tool_schema import (
    _ARGUMENT_VALIDATION_SECONDS,
    ToolSchemaError,
    validate_tool_arguments,
    validate_tool_schema,
)
from nomos.level_0.types.tool_call import ToolCallRequest
from nomos.level_0.types.tool_definition import (
    TOOL_CATALOG_MAX_BYTES,
    TOOL_CATALOG_MAX_TOOLS,
    TrustedToolDefinition,
    TrustedToolReviewContext,
    canonical_definition_json,
)

# Shared by services, with lazy threads and process lifetime. Never use the
# event loop's default executor for worker-slot waits or schema validation.
_RESOLVER_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tool-catalog")


class ToolCatalogError(ValueError):
    """Invalid council catalog or invocation; there is no legacy fallback."""


def resolve_tool_review_context(
    *,
    council_id: str,
    tools: tuple[TrustedToolDefinition, ...] | None,
    tool_call: ToolCallRequest | None = None,
    deadline: float | None = None,
) -> TrustedToolReviewContext | None:
    """Snapshot availability and validate any exact invocation before review."""
    if deadline is not None and time.monotonic() >= deadline:
        raise ToolCatalogError("tool catalog resolution deadline exceeded")
    if tools is None:
        return None
    try:
        if not isinstance(tools, tuple) or len(tools) > TOOL_CATALOG_MAX_TOOLS:
            raise ToolCatalogError("tool catalog must contain at most 32 definitions")
        names: set[str] = set()
        definitions = []
        for tool in tools:
            if not isinstance(tool, TrustedToolDefinition) or tool.name in names:
                raise ToolCatalogError("tool catalog contains invalid or duplicate definitions")
            names.add(tool.name)
            public = tool.to_public_dict()
            validate_tool_schema(public["parameters"])
            definitions.append(public)
        catalog_json = canonical_definition_json(definitions)
        if len(catalog_json.encode("utf-8")) > TOOL_CATALOG_MAX_BYTES:
            raise ToolCatalogError("tool catalog exceeds byte limit")
        if tool_call is None:
            return TrustedToolReviewContext(council_id, catalog_json, None, None, None)
        # Take an independent current request snapshot so schema checking,
        # reviewer arguments and the existing signed operation bind identical data.
        call = ToolCallRequest.model_validate(tool_call.model_dump(mode="python"))
        matched = next((tool for tool in definitions if tool["name"] == call.tool_name), None)
        if matched is None:
            raise ToolCatalogError("tool name is not in this council's catalog")
        if deadline is None:
            validate_tool_arguments(matched["parameters"], call.arguments)
        else:
            validate_tool_arguments(matched["parameters"], call.arguments, deadline=deadline)
        return TrustedToolReviewContext(
            council_id=council_id,
            catalog_json=catalog_json,
            matched_tool_name=call.tool_name,
            arguments_json=call.canonical_arguments().decode("utf-8"),
            operation=call.to_operation(),
        )
    except ToolCatalogError:
        raise
    except (ToolSchemaError, ValueError, TypeError, RecursionError):
        raise ToolCatalogError("invalid tool catalog or arguments") from None


class AsyncToolCatalogResolver:
    """Service-owned admission to the process-wide bounded resolver executor."""

    def __init__(self) -> None:
        self._admission = asyncio.BoundedSemaphore(4)

    async def resolve(
        self,
        *,
        council_id: str,
        tools: tuple[TrustedToolDefinition, ...] | None,
        tool_call: ToolCallRequest | None = None,
    ) -> TrustedToolReviewContext | None:
        deadline = time.monotonic() + _ARGUMENT_VALIDATION_SECONDS
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(
                self._admission.acquire(), timeout=max(0.0, deadline - time.monotonic())
            )
        except TimeoutError:
            raise ToolCatalogError("tool catalog resolution deadline exceeded") from None

        # Only this pre-submission path owns immediate release. Once submitted,
        # the concurrent future owns it, even if the asyncio caller goes away.
        try:
            if time.monotonic() >= deadline:
                raise ToolCatalogError("tool catalog resolution deadline exceeded")
            context = contextvars.copy_context()
            work = partial(
                resolve_tool_review_context,
                council_id=council_id,
                tools=tools,
                tool_call=tool_call,
                deadline=deadline,
            )
            future = _RESOLVER_EXECUTOR.submit(context.run, work)
        except Exception:
            self._admission.release()
            raise ToolCatalogError("tool catalog resolution unavailable or failed") from None

        def completed(_future: Future[TrustedToolReviewContext | None]) -> None:
            try:
                loop.call_soon_threadsafe(self._admission.release)
            except RuntimeError:
                # A closed loop cannot serve another request; never join a
                # running worker or touch an asyncio primitive from its thread.
                pass

        future.add_done_callback(completed)
        wrapped = asyncio.wrap_future(future, loop=loop)
        try:
            result = await asyncio.wait_for(
                asyncio.shield(wrapped), timeout=max(0.0, deadline - time.monotonic())
            )
            if time.monotonic() >= deadline:
                raise ToolCatalogError("tool catalog resolution deadline exceeded")
            return result
        except asyncio.CancelledError:
            future.cancel()  # Only queued work can be cancelled here.
            raise
        except TimeoutError:
            future.cancel()
            raise ToolCatalogError("tool catalog resolution deadline exceeded") from None
        except ToolCatalogError:
            raise
        except Exception:
            raise ToolCatalogError("tool catalog resolution unavailable or failed") from None
