# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Explicit operator trust in a host's completed human-approval checks.

Nomos owns and validates the assertion under Relay execution_context.data.
Relay transports opaque application data; it does not authenticate it. The host
must validate the human decision, principal, freshness and exact operation.
"""

from __future__ import annotations

import json
import math
from importlib.resources import files

from jsonschema import Draft202012Validator

from .settings import Settings

MAX_CONTEXT_BYTES = 16_384
_VALIDATOR = Draft202012Validator(
    json.loads((files(__package__) / "host-approval.schema.json").read_text(encoding="utf-8"))
)


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


def host_approval_requested(context: object) -> bool:
    """Select a supplied approval without treating malformed or legacy data as absence.

    Relay normally validates its concrete envelope before middleware. Repeat
    its shape/resource checks here so older SDKs and direct callers fail closed
    rather than turning an invalid host replay into an ordinary Council request.
    Application data without the host_approval key follows the ordinary path.
    """
    if context is None:
        return False
    if not isinstance(context, dict) or context.keys() - {"data"}:
        raise HostApprovalError("host_approval_invalid")
    data = context.get("data", {})
    if not isinstance(data, dict):
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
    return "host_approval" in data


def approval_id(context: object, tool_name: str, arguments: dict, settings: Settings) -> str:
    """Validate the Nomos-owned claim against configured trust and the held payload."""
    trust = settings.host_approval
    if trust.source is None or tool_name not in trust.tools:
        raise HostApprovalError("host_approval_untrusted")
    if not isinstance(context, dict) or not host_approval_requested(context):
        raise HostApprovalError("host_approval_invalid")
    proof = context["data"]["host_approval"]
    # The packaged Nomos schema owns assertion shape, including the reserved
    # obsolete decision key. Relay deliberately does not interpret this payload.
    # Extra application fields never replace the operational checks below.
    if not _VALIDATOR.is_valid(proof):
        raise HostApprovalError("host_approval_invalid")
    if proof["source"] != trust.source:
        raise HostApprovalError("host_approval_untrusted")
    if proof["tool_name"] != tool_name or settings.policy(tool_name).executor != "local":
        raise HostApprovalError("host_approval_operation_mismatch")
    if not same_json(arguments, proof["approved_arguments"]):
        raise HostApprovalError("host_approval_operation_mismatch")
    return proof["approval_id"]
