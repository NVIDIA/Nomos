# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Bounded facts about a literal simple command, not executable authorization.

Any literal executable name or path can be described. Assignments, reserved
shell syntax, expansion and composition remain unsupported; policy decides
whether the resulting executable/arguments are forbidden or need review.
Neither tokenization nor an absolute path establishes what a program will do.
The application still owns its folder and runtime execution boundary.
"""

from __future__ import annotations

import io
import re
import shlex
from typing import TypedDict

MAX_COMMAND_CHARS = 16_384
MAX_COMMAND_WORDS = 4096

# Grammar prefixes, not an executable allowlist. Quoted/escaped spellings may
# conservatively require review because shlex does not retain quote provenance.
_RESERVED_PREFIXES = {
    "!",
    "time",
    "coproc",
    "if",
    "then",
    "elif",
    "else",
    "fi",
    "for",
    "select",
    "in",
    "do",
    "done",
    "while",
    "until",
    "case",
    "esac",
    "function",
}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
_UNSUPPORTED = re.compile(r"[$`\n\r\x00(){}<>|&;*?\[\]~]")


class ShellCommand(TypedDict):
    """Proposed executable spelling and arguments; no filesystem identity claim."""

    executable: str
    name: str
    argv: list[str]


class ShellAnalysis(TypedDict):
    """Finite JSON facts consumed by Rego, independently of its policy decision."""

    supported: bool
    commands: list[ShellCommand]
    reason: str


class _CharacterLimitError(ValueError):
    """Stop lexing before a truncated token could be mistaken for a whole word."""


class _CommandReader(io.StringIO):
    """Bound lexer input while preserving only words completed before the limit.

    A synthetic EOF could make a partial option look complete. Raising instead
    lets shlex discard the unfinished word while earlier yielded words survive.
    The ordinary EOF of a command within the bound retains normal semantics.
    """

    def __init__(self, command: str):
        super().__init__(command[:MAX_COMMAND_CHARS])
        self.truncated = len(command) > MAX_COMMAND_CHARS

    def read(self, size: int | None = -1) -> str:
        if size == 0:
            return ""
        if self.truncated and self.tell() >= MAX_COMMAND_CHARS:
            raise _CharacterLimitError
        return super().read(size)


def analyze_shell_command(command: object) -> ShellAnalysis:
    """Describe a bounded single command without restricting executable names.

    Bare names, absolute paths and cwd-relative paths are all eligible for
    policy/model review. No execution, filesystem or PATH lookup occurs here.
    Unsupported syntax, incomplete parsing or a resource limit never produces
    ``supported=True``. Proven literal prefixes remain available for deny-only
    rules even when parsing later fails; partial words are never retained.
    """
    unknown: ShellAnalysis = {
        "supported": False,
        "commands": [],
        "reason": "Command requires human interpretation.",
    }
    if not isinstance(command, str) or not command.strip():
        return unknown
    stream = _CommandReader(command)
    lexer = shlex.shlex(stream, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    words: list[str] = []
    complete = not stream.truncated
    try:
        for word in lexer:
            if len(words) >= MAX_COMMAND_WORDS:
                complete = False
                break
            words.append(word)
    except ValueError:
        complete = False
    if not words:
        return unknown
    executable = words[0]
    name = executable.rsplit("/", 1)[-1]
    if (
        not name
        or executable in _RESERVED_PREFIXES
        or _ASSIGNMENT.match(executable)
        or _UNSUPPORTED.search(executable)
        or "#" in executable
    ):
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
    supported = complete and not _UNSUPPORTED.search(command) and "#" not in command
    return {
        "supported": bool(supported),
        "commands": [
            {
                "executable": executable,
                "name": name,
                "argv": words[1:] if supported else literal_prefix,
            }
        ],
        "reason": "" if supported else unknown["reason"],
    }
