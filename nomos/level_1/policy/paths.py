# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Path extraction helpers for deterministic policy checks."""

from __future__ import annotations

import posixpath
import shlex

SENSITIVE_PATHS = frozenset(
    {
        "/etc/shadow",
        "/app/keys",
        "/root",
        ".ssh",
        ".aws",
        ".gnupg",
    }
)

_SHELL_WORD_EDGE_CHARS = " \t\r\n\"'`;,|&()<>"


def normalized_path_references(
    command: str,
    *,
    working_directory: str = "",
    target_resource: str = "",
) -> list[str]:
    """Extract normalized path-like references from command metadata."""
    refs: set[str] = set()
    cwd = _normalize_path(working_directory)

    for value in (working_directory, target_resource):
        normalized = _normalize_path(value)
        if normalized:
            refs.add(normalized)

    for token in _shell_tokens(command):
        candidate = token.strip(_SHELL_WORD_EDGE_CHARS)
        if not _looks_path_like(candidate):
            continue
        normalized = _normalize_path(candidate, cwd=cwd)
        if normalized:
            refs.add(normalized)

    return sorted(refs)


def references_sensitive_path(
    command: str,
    *,
    working_directory: str = "",
    target_resource: str = "",
    sensitive_paths: set[str] | frozenset[str] = SENSITIVE_PATHS,
) -> bool:
    """Return True when command metadata references a sensitive path."""
    refs = normalized_path_references(
        command,
        working_directory=working_directory,
        target_resource=target_resource,
    )
    return any(_path_matches(ref, sensitive) for ref in refs for sensitive in sensitive_paths)


def _path_matches(path: str, sensitive: str) -> bool:
    if sensitive.startswith("/"):
        return path == sensitive or path.startswith(f"{sensitive}/")
    parts = [part for part in path.split("/") if part]
    return sensitive in parts


def _shell_tokens(command: str) -> list[str]:
    if not command:
        return []
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return command.split()


def _looks_path_like(value: str) -> bool:
    return bool(value) and ("/" in value or value.startswith(".") or "." in value)


def _normalize_path(value: str, *, cwd: str = "") -> str:
    value = value.strip(_SHELL_WORD_EDGE_CHARS)
    if not value:
        return ""
    if value.startswith("/"):
        return posixpath.normpath(value)
    if cwd and cwd.startswith("/") and _looks_path_like(value):
        return posixpath.normpath(posixpath.join(cwd, value))
    if value.startswith("."):
        return posixpath.normpath(value)
    return value
