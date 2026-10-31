# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Structured tool-call authorization material shared by request and writ handling."""

from __future__ import annotations

import json
from dataclasses import dataclass

from .wire import sha256_hex

TOOL_ARGUMENTS_MAX_BYTES = 32_000
TOOL_ARGUMENTS_MAX_INTEGER_DIGITS = 640
_TOOL_INTEGER_LIMIT = 10**TOOL_ARGUMENTS_MAX_INTEGER_DIGITS
TOOL_CALL_KIND = "tool_call"
TOOL_CALL_SCHEMA_VERSION = 1
TOOL_CALL_CANONICALIZATION = "nomos-json-v1"


def canonical_tool_arguments(arguments: dict) -> bytes:
    """Return the normative bytes used by Nomos's tool-call digest.

    nomos-json-v1 uses Python json.dumps number encoding: integers retain
    their exact value up to 640 decimal digits (excluding the sign) and
    floats retain Python's representation (including
    1.0). It is not RFC 8785/JCS; other-language clients must reproduce these
    bytes rather than use their native JSON serializer.
    """

    pending = [arguments]
    visited: set[int] = set()
    while pending:
        value = pending.pop()
        if isinstance(value, int) and not isinstance(value, bool):
            if not -_TOOL_INTEGER_LIMIT < value < _TOOL_INTEGER_LIMIT:
                raise ValueError("tool_integer_too_large")
        elif isinstance(value, dict | list | tuple) and id(value) not in visited:
            visited.add(id(value))
            pending.extend(value.values() if isinstance(value, dict) else value)
    encoded = json.dumps(
        arguments,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > TOOL_ARGUMENTS_MAX_BYTES:
        raise ValueError("tool_arguments_too_large")
    return encoded


@dataclass(frozen=True)
class StructuredToolCall:
    """Immutable exact arguments and their persistable signed operation binding."""

    operation: dict
    arguments: dict
    arguments_bytes: bytes

    @classmethod
    def build(cls, tool_name: str, arguments: dict, subject: str) -> StructuredToolCall:
        """Canonicalize business arguments once and bind their exact UTF-8 bytes."""

        encoded = canonical_tool_arguments(arguments)
        snapshot = json.loads(encoded)
        operation = {
            "kind": TOOL_CALL_KIND,
            "schema_version": TOOL_CALL_SCHEMA_VERSION,
            "tool_name": tool_name,
            "arguments_sha256": sha256_hex(encoded),
            "canonicalization": TOOL_CALL_CANONICALIZATION,
            "subject": subject,
        }
        return cls(operation, snapshot, encoded)

    def authorization(self) -> dict:
        """Return the live Council input; exact arguments are not part of the signed writ."""

        # Return a copy so an injected/in-process Council implementation cannot
        # mutate the operation snapshot that the handler later verifies.
        # Reparse the canonical snapshot so an in-process Council cannot mutate
        # the arguments later dispatched by the plugin.
        return {
            "schema_version": TOOL_CALL_SCHEMA_VERSION,
            "tool_name": self.operation["tool_name"],
            "arguments": json.loads(self.arguments_bytes),
            "subject": self.operation["subject"],
        }
