# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Duplicate-member checks at boundaries that still receive raw JSON.

Already decoded dictionaries (including MCP/Relay and protobuf Struct inputs)
cannot reveal members discarded by their upstream JSON parser.
"""

from __future__ import annotations

import json
from typing import Any, Literal, Self

from pydantic import BaseModel, ValidationError


class DuplicateJsonMemberError(ValueError):
    """A raw JSON object repeats a member name."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            # Names and values may contain secrets; never include them in errors.
            raise DuplicateJsonMemberError("duplicate JSON object member")
        result[key] = value
    return result


def strict_json_loads(data: str | bytes | bytearray) -> Any:
    """Decode JSON while rejecting duplicate members at every object depth."""

    return json.loads(data, object_pairs_hook=_unique_object)


class DuplicateSafeJsonModel(BaseModel):
    """Precheck raw JSON without changing Pydantic's JSON-mode conversions."""

    @classmethod
    def model_validate_json(
        cls,
        json_data: str | bytes | bytearray,
        *,
        strict: bool | None = None,
        extra: Literal["allow", "ignore", "forbid"] | None = None,
        context: Any | None = None,
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Self:
        """Reject duplicate members before normal raw-JSON model validation."""

        try:
            # The precheck discards its result: defer all numeric conversion to
            # Pydantic, including its handling of large integers and floats.
            json.loads(json_data, object_pairs_hook=_unique_object, parse_int=str, parse_float=str)
        except DuplicateJsonMemberError as exc:
            raise ValidationError.from_exception_data(
                cls.__name__,
                [{"type": "json_invalid", "loc": (), "input": None, "ctx": {"error": str(exc)}}],
            ) from None
        except (ValueError, UnicodeDecodeError, RecursionError):
            # Keep Pydantic's established diagnostics for malformed raw JSON.
            pass
        options: dict[str, Any] = {"strict": strict, "context": context}
        # Older supported Pydantic releases do not expose these optional flags.
        # Forward them only when requested, rather than passing new defaults
        # into an older superclass signature.
        for name, value in (("extra", extra), ("by_alias", by_alias), ("by_name", by_name)):
            if value is not None:
                options[name] = value
        return super().model_validate_json(json_data, **options)
