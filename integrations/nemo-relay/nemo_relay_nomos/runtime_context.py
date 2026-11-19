# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Explicit host runtime facts, bound to one call and never treated as approval."""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from .host_approval import HostApprovalError, host_approval_requested
from .settings import Settings
from .tool_call import canonical_tool_arguments


class RuntimeContextError(ValueError):
    """A supplied host context cannot safely be used; never fall back silently."""


def _path(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 4096
        and value == value.strip()
        and not re.search(r"[\x00-\x1f\x7f]", value)
        and (PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute())
        and not any(part in {".", ".."} for part in re.split(r"[/\\]", value))
    )


def _root_keys(value: str) -> set[tuple[str, str]]:
    """Compare all valid lexical interpretations, without assuming the host OS.

    Forward-slash UNC paths can also be POSIX paths. Keep both identities so a
    second spelling cannot hide an overlapping root. Windows comparison is
    conservatively case-insensitive; this never changes the reported paths.
    """
    keys: set[tuple[str, str]] = set()
    if PureWindowsPath(value).is_absolute():
        keys.add(("windows", PureWindowsPath(value).as_posix().rstrip("/").casefold()))
    if PurePosixPath(value).is_absolute():
        keys.add(("posix", "/" + "/".join(part for part in value.split("/") if part)))
    return keys


def runtime_context(ctx: Any, arguments: dict, settings: Settings) -> dict | None:
    """Validate the host envelope and strip its duplicate argument/ID binding.

    Relay transports application data, not proof of its source. The operator
    explicitly trusts this issuer for exact tools; Council checks its own
    authenticated issuer policy. Paths describe resolution, not confinement,
    filesystem observations or permission to invoke the continuation.
    """
    envelope = getattr(ctx, "execution_context", None)
    # Leave generic/approval-only envelope errors to the existing host-approval
    # admission path. This parser owns only a supplied runtime_context field.
    if (
        not isinstance(envelope, dict)
        or not isinstance(envelope.get("data"), dict)
        or "runtime_context" not in envelope["data"]
    ):
        return None
    try:
        host_approval_requested(envelope)  # shared strict JSON/depth/16 KiB envelope admission
    except HostApprovalError as exc:
        raise RuntimeContextError("runtime_context_invalid") from exc
    data = envelope.get("data", {}) if isinstance(envelope, dict) else {}
    if "runtime_context" not in data:
        return None
    raw = data["runtime_context"]
    trust = settings.runtime_context
    if trust.source is None or ctx.tool_name not in trust.tools:
        raise RuntimeContextError("runtime_context_untrusted")
    if (
        not isinstance(raw, dict)
        or set(raw) != {"version", "source", "tool_name", "tool_call_id", "arguments", "runtime"}
        or type(raw["version"]) is not int
        or raw["version"] != 1
    ):
        raise RuntimeContextError("runtime_context_invalid")
    if raw["source"] != trust.source:
        raise RuntimeContextError("runtime_context_untrusted")
    try:
        exact_arguments = canonical_tool_arguments(arguments) == canonical_tool_arguments(raw["arguments"])
    except (TypeError, ValueError, RecursionError):
        exact_arguments = False
    if (
        raw["tool_name"] != ctx.tool_name
        or not isinstance(ctx.tool_call_id, str)
        or not 1 <= len(ctx.tool_call_id) <= 256
        or raw["tool_call_id"] != ctx.tool_call_id
        or not exact_arguments
    ):
        raise RuntimeContextError("runtime_context_operation_mismatch")
    runtime = raw["runtime"]
    if not isinstance(runtime, dict) or set(runtime) - {"working_directory", "workspace_mappings"}:
        raise RuntimeContextError("runtime_context_invalid")
    cwd = runtime.get("working_directory")
    mappings = runtime.get("workspace_mappings", [])
    if cwd is not None and not _path(cwd):
        raise RuntimeContextError("runtime_context_invalid")
    if not isinstance(mappings, list) or len(mappings) > 16 or (cwd is None and not mappings):
        raise RuntimeContextError("runtime_context_invalid")
    roots: set[tuple[str, str]] = set()
    for item in mappings:
        if (
            not isinstance(item, dict)
            or set(item) != {"logical_root", "execution_root"}
            or not _path(item["logical_root"])
            or not _path(item["execution_root"])
        ):
            raise RuntimeContextError("runtime_context_invalid")
        keys = _root_keys(item["logical_root"])
        if any(
            style == other_style
            and (
                root == other
                or root.startswith(other.rstrip("/") + "/")
                or other.startswith(root.rstrip("/") + "/")
            )
            for style, root in keys
            for other_style, other in roots
        ):
            raise RuntimeContextError("runtime_context_invalid")
        roots.update(keys)
    return json.loads(json.dumps({"version": 1, "source": raw["source"], "runtime": runtime}))
