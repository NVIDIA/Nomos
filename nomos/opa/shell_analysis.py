# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Bounded static shell facts for policy, not a Bash interpreter or sandbox.

Only one literal simple command is recognized for automated review. Expansion,
redirection, shell composition and opaque interpreters require human review
unless a proven literal prefix already establishes a hard prohibition. Shlex
tokenization alone cannot establish Bash semantics. The application must still
enforce its folder and execution boundary.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

# These executable identities identify policy categories, not safe operations.
# Council still reviews every ordinary command that passes this filter.
_COMMANDS = {
    ":",
    "true",
    "false",
    "printf",
    "echo",
    "pwd",
    "ls",
    "cat",
    "mkdir",
    "cp",
    "mv",
    "touch",
    "head",
    "tail",
    "wc",
    "cd",
    "rm",
    "sudo",
    "su",
    "chmod",
}
_UNSUPPORTED = re.compile(r"[$`\n\r\x00(){}<>|&;*?\[\]~]")


def analyze_shell_command(command: Any) -> dict[str, Any]:
    """Return finite JSON facts, routing unrecognized syntax to human review.

    Quotes and backslash-escaped literal words are handled by POSIX shlex.
    Metacharacters make the overall command unsupported even inside quotes. This can ask
    for review of harmless literals; it cannot silently authorize expansion.
    No filesystem lookup, command execution, environment expansion, or PATH
    resolution occurs here. Inherited shell functions remain a host concern.
    """
    unknown = {
        "supported": False,
        "commands": [],
        "reason": "Command requires human interpretation.",
    }
    if not isinstance(command, str) or not command.strip() or len(command) > 16_384:
        return unknown
    try:
        words = shlex.split(command, posix=True, comments=False)
    except ValueError:
        return unknown
    if not words or len(words) > 4096:
        return unknown
    executable = words[0]
    name = executable.rsplit("/", 1)[-1]
    if name not in _COMMANDS:
        return unknown
    # Avoid treating assignments, relative scripts, or arbitrary executables
    # that happen to have a familiar basename as a standard command.
    if "/" in executable and executable not in (f"/bin/{name}", f"/usr/bin/{name}"):
        return unknown
    # Retain only a proven literal prefix for deny-only rules. For example,
    # rm -rf "$TARGET" is opaque overall, but its executable and destructive
    # options are already unambiguous. Stop before any expansion/separator;
    # an earlier dynamic word might expand to -- and change option parsing.
    literal_prefix = []
    for word in words[1:]:
        if _UNSUPPORTED.search(word) or "#" in word:
            break
        literal_prefix.append(word)
    supported = not _UNSUPPORTED.search(command) and "#" not in command
    return {
        "supported": bool(supported),
        "commands": [{"name": name, "argv": words[1:] if supported else literal_prefix}],
        "reason": "" if supported else unknown["reason"],
    }
