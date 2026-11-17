# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Explicit operator trust in a host's completed human-approval checks.

Relay transports this object; it does not authenticate its contents. The host
must validate the human decision, principal, freshness and exact operation.
"""

from __future__ import annotations

import json
import math

from .settings import Settings

MAX_CONTEXT_BYTES = 16_384
_FIELDS = {"version", "source", "decision", "approval_id", "tool_name", "approved_arguments"}


class HostApprovalError(ValueError):
    """A bounded refusal reason that never contains approval data."""


def same_json(left: object, right: object) -> bool:
    """Compare JSON structurally, keeping booleans distinct from numbers."""
    if type(left) is bool or type(right) is bool:
        return type(left) is type(right) and left == right
    if type(left) in (int, float) and type(right) in (int, float):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(same_json(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(same_json(a, b) for a, b in zip(left, right, strict=True))
    return left == right


def _json_value(value: object) -> bool:
    """Bound extension depth and accept only finite, string-keyed JSON values."""
    pending = [(value, 0)]
    while pending:
        item, parent_depth = pending.pop()
        if isinstance(item, (dict, list)):
            depth = parent_depth + 1
            if depth > 64:
                return False
            if isinstance(item, dict):
                if any(type(key) is not str for key in item):
                    return False
                pending.extend((child, depth) for child in item.values())
            else:
                pending.extend((child, depth) for child in item)
        elif (
            item is not None
            and type(item) not in (str, bool, int, float)
            or type(item) is float
            and not math.isfinite(item)
        ):
            return False
    return True


def approval_id(context: object, tool_name: str, arguments: dict, settings: Settings) -> str:
    """Validate a host assertion against configured trust and the held payload."""
    trust = settings.host_approval
    if trust.source is None or tool_name not in trust.tools:
        raise HostApprovalError("host_approval_untrusted")
    if not isinstance(context, dict) or "host_approval" not in context:
        raise HostApprovalError("host_approval_invalid")
    try:
        if (
            not _json_value(context)
            or len(json.dumps(context, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode())
            > MAX_CONTEXT_BYTES
        ):
            raise HostApprovalError("host_approval_invalid")
    except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
        raise HostApprovalError("host_approval_invalid") from exc
    # Relay preserves additional properties; only the recognized assertion fields
    # participate in authorization. Unknown fields cannot replace these checks.
    proof = context["host_approval"]
    if not isinstance(proof, dict) or not _FIELDS.issubset(proof):
        raise HostApprovalError("host_approval_invalid")
    if type(proof["version"]) not in (int, float) or proof["version"] != 1:
        raise HostApprovalError("host_approval_invalid")
    if proof["source"] != trust.source or proof["decision"] != "approved":
        raise HostApprovalError("host_approval_untrusted")
    if proof["tool_name"] != tool_name or settings.policy(tool_name).executor != "local":
        raise HostApprovalError("host_approval_operation_mismatch")
    identity = proof["approval_id"]
    if not isinstance(identity, str) or not identity.strip() or len(identity) > 256:
        raise HostApprovalError("host_approval_invalid")
    if not isinstance(proof["approved_arguments"], dict) or not same_json(
        arguments, proof["approved_arguments"]
    ):
        raise HostApprovalError("host_approval_operation_mismatch")
    return identity
