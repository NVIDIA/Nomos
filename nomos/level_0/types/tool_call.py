# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Versioned operation binding for PA tool-call authorization."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Bound exact live-review arguments independently of advisory conversation
# context. Neither payload is included in serialized tool-call Intents.
TOOL_ARGUMENTS_MAX_BYTES = 32_000
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("tool arguments JSON contains duplicate object keys")
        result[key] = value
    return result


def _reject_non_finite(_: str) -> None:
    raise ValueError("tool arguments JSON contains a non-finite number")


def _validate_json_value(value: Any) -> None:
    """Reject values that are not portable JSON data."""

    if value is None or isinstance(value, str | bool | int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("tool arguments JSON contains a non-finite number")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("tool arguments JSON object keys must be strings")
            _validate_json_value(item)
        return
    raise ValueError("tool arguments JSON contains a non-JSON value")


class ToolCallOperation(BaseModel):
    """Privacy-minimized operation fields carried by a signed v2 writ.

    The arguments themselves are supplied only for the live Council review.
    Their digest is signed and persisted so PA can bind the writ to the exact
    held dispatch without making Nomos another durable copy of message bodies.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["pa2.tool_call"] = "pa2.tool_call"
    schema_version: Literal[1] = 1
    tool_name: str = Field(min_length=1, max_length=256, pattern=r"^[^\x00-\x1f\x7f]+$")
    arguments_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    subject: str = Field(
        min_length=1,
        max_length=512,
        description="Opaque, server-derived PA tenant/user subject",
    )


class ToolCallAuthorization(BaseModel):
    """Live Council-review material plus its persistable signed binding.

    SHA-256 binds the exact UTF-8 ``arguments_json`` bytes supplied by the
    caller. JSON structure is validated without reserializing those bytes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: ToolCallOperation
    arguments_json: str = Field(min_length=2, max_length=TOOL_ARGUMENTS_MAX_BYTES)

    @field_validator("arguments_json")
    @classmethod
    def validate_arguments_json(cls, value: str) -> str:
        encoded = value.encode("utf-8")
        if len(encoded) > TOOL_ARGUMENTS_MAX_BYTES:
            raise ValueError("tool arguments JSON exceeds the byte limit")
        try:
            parsed = json.loads(
                value,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_non_finite,
            )
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise ValueError("tool arguments JSON is malformed") from exc
        if not isinstance(parsed, dict):
            raise ValueError("tool arguments JSON must encode an object")
        _validate_json_value(parsed)
        return value

    @model_validator(mode="after")
    def validate_digest(self) -> ToolCallAuthorization:
        digest = hashlib.sha256(self.arguments_json.encode("utf-8")).hexdigest()
        if not _SHA256_RE.fullmatch(self.operation.arguments_sha256) or not hmac.compare_digest(
            digest, self.operation.arguments_sha256
        ):
            raise ValueError("tool arguments digest does not match arguments_json")
        return self
