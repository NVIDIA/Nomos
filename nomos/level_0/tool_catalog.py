# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Resolve an exact structured call against one operator-owned council catalog."""

from __future__ import annotations

from nomos.level_0.tool_schema import ToolSchemaError, validate_tool_arguments, validate_tool_schema
from nomos.level_0.types.tool_call import ToolCallRequest
from nomos.level_0.types.tool_definition import (
    TOOL_CATALOG_MAX_BYTES,
    TOOL_CATALOG_MAX_TOOLS,
    TrustedToolDefinition,
    TrustedToolReviewContext,
    canonical_definition_json,
)


class ToolCatalogError(ValueError):
    """Invalid council catalog or invocation; there is no legacy fallback."""


def resolve_tool_review_context(
    *,
    council_id: str,
    tools: tuple[TrustedToolDefinition, ...] | None,
    tool_call: ToolCallRequest | None = None,
) -> TrustedToolReviewContext | None:
    """Snapshot availability and validate any exact invocation before review."""
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
        validate_tool_arguments(matched["parameters"], call.arguments)
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
