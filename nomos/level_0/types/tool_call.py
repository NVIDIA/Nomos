# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Structured application tool-call authorization types."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

TOOL_ARGUMENTS_MAX_BYTES = 32_000
TOOL_ARGUMENTS_MAX_DEPTH = 64
TOOL_ARGUMENTS_MAX_NODES = 10_000
TOOL_CALL_CANONICALIZATION: Final[Literal["nomos-json-v1"]] = "nomos-json-v1"


def _validate_json_value(value: Any, *, depth: int = 0) -> int:
    """Validate portable JSON data and return its recursive node count."""

    if depth > TOOL_ARGUMENTS_MAX_DEPTH:
        raise ValueError("tool arguments exceed the nesting limit")
    if value is None or isinstance(value, str | bool | int):
        return 1
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("tool arguments contain a non-finite number")
        return 1
    if isinstance(value, list):
        return 1 + sum(_validate_json_value(item, depth=depth + 1) for item in value)
    if isinstance(value, dict):
        count = 1
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("tool argument object keys must be strings")
            count += _validate_json_value(item, depth=depth + 1)
        return count
    raise ValueError("tool arguments contain a non-JSON value")


def canonical_tool_arguments(arguments: dict[str, Any]) -> bytes:
    """Return the normative UTF-8 bytes used for tool-argument digests.

    ``nomos-json-v1`` is compact JSON with lexicographically sorted object
    keys, literal UTF-8 for Unicode text, and finite JSON numbers only.
    """

    if not isinstance(arguments, dict):
        raise ValueError("tool arguments must be an object")
    if _validate_json_value(arguments) > TOOL_ARGUMENTS_MAX_NODES:
        raise ValueError("tool arguments exceed the node limit")
    encoded = json.dumps(
        arguments,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > TOOL_ARGUMENTS_MAX_BYTES:
        raise ValueError("tool arguments exceed the byte limit")
    return encoded


class ToolCallOperation(BaseModel):
    """Server-derived operation binding persisted and signed by Nomos."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["tool_call"] = "tool_call"
    schema_version: Literal[1] = 1
    tool_name: str = Field(min_length=1, max_length=256, pattern=r"^[^\x00-\x1f\x7f]+$")
    arguments_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonicalization: Literal["nomos-json-v1"] = TOOL_CALL_CANONICALIZATION
    subject: str = Field(
        min_length=1,
        max_length=512,
        description="Opaque, host-derived tenant or user subject",
    )

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: object) -> object:
        """Reject bool and float coercion at the wire boundary."""

        if type(value) is not int or value != 1:
            raise ValueError("tool-call schema_version must be integer 1")
        return value


class ToolCallRequest(BaseModel):
    """Live structured tool call received by ``council_authorize``.

    The caller supplies the operation it intends to dispatch. Nomos validates
    and snapshots the arguments, then derives the digest carried by the writ.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    tool_name: str = Field(min_length=1, max_length=256, pattern=r"^[^\x00-\x1f\x7f]+$")
    arguments: dict[str, Any]
    subject: str = Field(min_length=1, max_length=512)

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: object) -> object:
        """Reject bool and float coercion at the wire boundary."""

        if type(value) is not int or value != 1:
            raise ValueError("tool-call schema_version must be integer 1")
        return value

    @field_validator("arguments", mode="before")
    @classmethod
    def validate_and_snapshot_arguments(cls, value: object) -> dict[str, Any]:
        """Validate, bound, and deep-copy the exact business arguments."""

        if not isinstance(value, dict):
            raise ValueError("tool arguments must be an object")
        encoded = canonical_tool_arguments(value)
        return cast(dict[str, Any], json.loads(encoded))

    def canonical_arguments(self) -> bytes:
        """Return the canonical bytes reviewed and bound into the writ."""

        return canonical_tool_arguments(self.arguments)

    def to_operation(self) -> ToolCallOperation:
        """Derive the privacy-minimized operation signed by Nomos."""

        return ToolCallOperation(
            schema_version=self.schema_version,
            tool_name=self.tool_name,
            arguments_sha256=hashlib.sha256(self.canonical_arguments()).hexdigest(),
            subject=self.subject,
        )
