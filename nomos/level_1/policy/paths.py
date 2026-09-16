# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


"""Path extraction helpers for deterministic policy checks."""

from __future__ import annotations

import fnmatch
import posixpath
import re
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
_GLOB_CHARS = "*?[{"
_LEADING_SLASHES = re.compile(r"^/{2,}")


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
        if path == sensitive or path.startswith(f"{sensitive}/"):
            return True
        return _glob_matches_absolute(path, sensitive)
    parts = [part for part in path.split("/") if part]
    if sensitive in parts:
        return True
    return any(_glob_matches_name(part, sensitive) for part in parts)


def _is_glob(value: str) -> bool:
    return any(char in value for char in _GLOB_CHARS)


def _glob_prefix(value: str) -> str:
    """Return the literal text before the first glob metacharacter."""
    index = min(value.index(char) for char in _GLOB_CHARS if char in value)
    return value[:index]


def _glob_matches_absolute(path: str, sensitive: str) -> bool:
    """Conservatively decide whether a glob token can expand to a sensitive root.

    The executor runs metacharacter commands under ``sh -c``, so a glob is a
    reference to everything it could expand to. Three cases count as a match:
    the glob names something beneath the sensitive root, the glob's literal
    prefix is itself a prefix of the sensitive root (so an expansion could
    land on it), or the glob pattern matches the root outright.
    """
    if not _is_glob(path):
        return False
    prefix = _glob_prefix(path)
    if prefix.startswith(f"{sensitive}/"):
        return True
    if prefix and sensitive.startswith(prefix):
        return True
    return fnmatch.fnmatchcase(sensitive, path)


def _glob_matches_name(part: str, sensitive: str) -> bool:
    """Decide whether one glob path component can expand to a sensitive name."""
    if not _is_glob(part):
        return False
    if sensitive.startswith(".") and not part.startswith("."):
        # Shell globbing never expands a pattern without a leading dot into a
        # dotfile, so ``*`` and ``*.log`` cannot reach ``.ssh``; ``.*`` can.
        return False
    prefix = _glob_prefix(part)
    if prefix and sensitive.startswith(prefix):
        return True
    return fnmatch.fnmatchcase(sensitive, part)


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
        return _collapse_leading_slashes(posixpath.normpath(value))
    if cwd and cwd.startswith("/") and _looks_path_like(value):
        return _collapse_leading_slashes(posixpath.normpath(posixpath.join(cwd, value)))
    if value.startswith("."):
        return posixpath.normpath(value)
    return value


def _collapse_leading_slashes(value: str) -> str:
    """Fold ``//x`` to ``/x``.

    POSIX lets ``normpath`` keep exactly two leading slashes as an
    implementation-defined prefix; Linux resolves them as one. The policy must
    see the path the kernel will open.
    """
    return _LEADING_SLASHES.sub("/", value)
