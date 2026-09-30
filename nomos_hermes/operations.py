# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Exact operation extraction for the supported Hermes tool surface."""

from __future__ import annotations

import posixpath
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

GOVERNED_TOOL = "terminal"

# These tools can execute or mutate local state without going through terminal.
# Writ v1 cannot faithfully bind their structured semantics, so an enabled
# Nomos profile blocks them instead of pretending they are covered.
UNSUPPORTED_LOCAL_MUTATION_TOOLS = frozenset(
    {
        "browser_exec",
        "cronjob_manage",
        "execute_code",
        "patch",
        "process",
        "process_manage",
        "setup_mcp",
        "skill_manage",
        "write_file",
    }
)


class UnsupportedOperationError(ValueError):
    """A safe reason why a Hermes operation cannot be represented by writ v1."""


@dataclass(frozen=True)
class TerminalOperation:
    """Exact terminal command and working directory bound into a writ."""

    command: str
    working_directory: str


def extract_terminal_operation(tool_name: str, args: Mapping[str, Any]) -> TerminalOperation | None:
    """Return the exact supported operation, ``None`` for an unrelated tool.

    V1 intentionally accepts only foreground terminal calls with an explicit,
    canonical absolute POSIX working directory. Any additional option changes
    execution semantics that the current command/cwd writ does not sign.
    """

    if tool_name in UNSUPPORTED_LOCAL_MUTATION_TOOLS:
        raise UnsupportedOperationError(
            f"{tool_name} is disabled while Nomos writ v1 enforcement is enabled"
        )
    if tool_name != GOVERNED_TOOL:
        return None
    if not isinstance(args, Mapping):
        raise UnsupportedOperationError("terminal arguments must be an object")
    if set(args) != {"command", "workdir"}:
        raise UnsupportedOperationError(
            "Nomos writ v1 requires terminal(command, workdir) with no additional options"
        )
    command = args.get("command")
    workdir = args.get("workdir")
    if not isinstance(command, str) or not command:
        raise UnsupportedOperationError("terminal command must be non-empty text")
    if not isinstance(workdir, str) or not workdir.startswith("/"):
        raise UnsupportedOperationError("terminal workdir must be an explicit absolute POSIX path")
    if "\x00" in command or "\x00" in workdir or posixpath.normpath(workdir) != workdir:
        raise UnsupportedOperationError("terminal command or workdir is not canonical")
    return TerminalOperation(command=command, working_directory=workdir)
