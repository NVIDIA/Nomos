# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Operator-defined tool values and immutable, request-local review snapshots."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nomos.level_0.types.tool_call import ToolCallOperation

TOOL_SCHEMA_MAX_BYTES = 32_000
TOOL_SCHEMA_MAX_DEPTH = 64
TOOL_SCHEMA_MAX_NODES = 10_000
TOOL_CATALOG_MAX_BYTES = 128_000
TOOL_CATALOG_MAX_TOOLS = 32


def canonical_definition_json(value: Any) -> str:
    """Encode portable JSON without coercion, templates or nonfinite values."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def snapshot_tool_schema(value: Any) -> dict[str, Any]:
    """Bound and snapshot schema JSON, including inert annotation data."""
    count = 0
    active: set[int] = set()

    def visit(item: Any, depth: int) -> None:
        nonlocal count
        count += 1
        if depth > TOOL_SCHEMA_MAX_DEPTH or count > TOOL_SCHEMA_MAX_NODES:
            raise ValueError("tool schema exceeds nesting or node limit")
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if isinstance(item, dict | list):
            identity = id(item)
            if identity in active:
                raise ValueError("tool schema contains a cycle")
            active.add(identity)
            if isinstance(item, dict):
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise ValueError("tool schema object keys must be strings")
                    visit(child, depth + 1)
            else:
                for child in item:
                    visit(child, depth + 1)
            active.remove(identity)
            return
        raise ValueError("tool schema must contain finite JSON values only")

    if not isinstance(value, dict):
        raise ValueError("tool parameters must be an object schema")
    visit(value, 0)
    encoded = canonical_definition_json(value)
    if len(encoded.encode("utf-8")) > TOOL_SCHEMA_MAX_BYTES:
        raise ValueError("tool schema exceeds byte limit")
    return cast(dict[str, Any], json.loads(encoded))


def _immutable(*args: Any, **kwargs: Any) -> Any:
    raise TypeError("trusted tool definitions are immutable")


class _FrozenDict(dict[str, Any]):
    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _immutable

    def __deepcopy__(self, memo: dict[int, Any]) -> _FrozenDict:
        return self


class _FrozenList(list[Any]):
    __setitem__ = __delitem__ = append = clear = extend = insert = pop = remove = _immutable
    reverse = sort = __iadd__ = __imul__ = _immutable

    def __deepcopy__(self, memo: dict[int, Any]) -> _FrozenList:
        return self


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return _FrozenDict({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenList(_freeze(item) for item in value)
    return value


class TrustedToolDefinition(BaseModel):
    """A complete, owned definition; schema semantics are checked by the adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(
        strict=True, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$"
    )
    description: str = Field(strict=True, min_length=1, max_length=8000)
    parameters: dict[str, Any]
    usage_constraints: tuple[str, ...]

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*", value) is None:
            raise ValueError("invalid trusted tool name")
        return value

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("tool description must not be blank")
        return value

    @field_validator("usage_constraints", mode="before")
    @classmethod
    def validate_constraints(cls, value: Any) -> tuple[str, ...]:
        if not isinstance(value, list | tuple) or not 1 <= len(value) <= 16:
            raise ValueError("tool usage_constraints requires 1 to 16 strings")
        if any(type(item) is not str or not item.strip() or len(item) > 2000 for item in value):
            raise ValueError(
                "tool usage constraints must be nonblank strings of at most 2000 characters"
            )
        return tuple(value)

    @field_validator("parameters", mode="before")
    @classmethod
    def snapshot_parameters(cls, value: Any) -> dict[str, Any]:
        return snapshot_tool_schema(value)

    @field_validator("parameters")
    @classmethod
    def freeze_parameters(cls, value: dict[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], _freeze(value))

    def to_public_dict(self) -> dict[str, Any]:
        """Return fresh JSON data; callers cannot mutate the definition."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": json.loads(canonical_definition_json(self.parameters)),
            "usage_constraints": list(self.usage_constraints),
        }


@dataclass(frozen=True)
class TrustedToolReviewContext:
    """Immutable council catalog and exact business arguments used in one review."""

    council_id: str
    catalog_json: str
    matched_tool_name: str | None
    arguments_json: str | None
    operation: ToolCallOperation | None

    def to_public_dict(self) -> dict[str, Any]:
        """Export complete matched semantics and independent mutable JSON copies."""
        tools = json.loads(self.catalog_json)
        matched = next((tool for tool in tools if tool["name"] == self.matched_tool_name), None)
        return {
            "council_id": self.council_id,
            "tools": tools,
            "matched_tool": json.loads(canonical_definition_json(matched)),
            "arguments": json.loads(self.arguments_json)
            if self.arguments_json is not None
            else None,
            "operation": self.operation.model_dump(mode="json")
            if self.operation is not None
            else None,
        }
