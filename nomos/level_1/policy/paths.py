# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Path extraction helpers for deterministic policy checks.

Two kinds of references are extracted from a command and its metadata:

* **literal path references** (:func:`normalized_path_references`): tokens
  that name a path outright. Quoted or backslash-escaped tokens are always
  literal, because the shell passes them through without pathname expansion,
  so ``grep '.*'`` is a regular expression, not a glob.
* **glob references** (:func:`normalized_glob_references`): unquoted tokens
  carrying a POSIX pathname-expansion metacharacter (``*``, ``?``, ``[``) that
  the executor's shell would expand at run time. Any such word is a path
  reference, whether or not it contains ``/`` or ``.``.

Glob grammar
    POSIX pathname expansion, evaluated with :func:`fnmatch.fnmatchcase`.
    Brace alternatives (``{a,b}``) are expanded here into separate references
    first, one level, no nesting, so downstream policy (this module and the
    Rego mirror alike) only ever sees ``* ? [ ]`` patterns. An alternative with
    no remaining metacharacter is a literal reference.

Relative globs
    Resolved against the working directory, or against the target resource
    when it is an absolute path and no working directory was given. A relative
    glob with neither is dropped: an unresolved relative pattern is never
    compared with absolute sensitive roots.

Limitation
    Quote classification tokenizes the command twice, in POSIX mode (quotes
    stripped) and non-POSIX mode (quotes kept). When the two tokenizations
    disagree on word count the command is treated as all-literal, which errs
    on the side of missing a glob rather than inventing one.
"""

from __future__ import annotations

import fnmatch
import posixpath
import re

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
_GLOB_ELIGIBLE_CHARS = "*?[{"
_GLOB_MATCH_CHARS = "*?["
_SHELL_CONTROL_CHARS = "();<>|&"
_LEADING_SLASHES = re.compile(r"^/{2,}")
_BRACE_GROUP = re.compile(r"\{([^{}]*,[^{}]*)\}")
_MAX_BRACE_ALTERNATIVES = 64
# Stands in for a brace pattern with more alternatives than the policy will
# enumerate; as a glob it reaches every absolute root, so the word fails closed.
_UNBOUNDED_GLOB = "/*"


def normalized_path_references(
    command: str,
    *,
    working_directory: str = "",
    target_resource: str = "",
) -> list[str]:
    """Extract normalized literal path references from command metadata."""
    literals, _globs = _extract_references(
        command, working_directory=working_directory, target_resource=target_resource
    )
    return sorted(literals)


def normalized_glob_references(
    command: str,
    *,
    working_directory: str = "",
    target_resource: str = "",
) -> list[str]:
    """Extract normalized, brace-expanded glob references from a command.

    Every returned pattern is absolute (resolved against the working directory
    or target resource) and contains at least one of ``* ? [``.
    """
    _literals, globs = _extract_references(
        command, working_directory=working_directory, target_resource=target_resource
    )
    return sorted(globs)


def references_sensitive_path(
    command: str,
    *,
    working_directory: str = "",
    target_resource: str = "",
    sensitive_paths: set[str] | frozenset[str] = SENSITIVE_PATHS,
) -> bool:
    """Return True when command metadata references a sensitive path."""
    literals, globs = _extract_references(
        command, working_directory=working_directory, target_resource=target_resource
    )
    if any(_literal_matches(ref, s) for ref in literals for s in sensitive_paths):
        return True
    return any(_glob_matches(ref, s) for ref in globs for s in sensitive_paths)


def _extract_references(
    command: str,
    *,
    working_directory: str,
    target_resource: str,
) -> tuple[set[str], set[str]]:
    literals: set[str] = set()
    globs: set[str] = set()
    cwd = _normalize_path(working_directory)
    target = _normalize_path(target_resource)

    for normalized in (cwd, target):
        if normalized:
            literals.add(normalized)

    if cwd.startswith("/"):
        glob_base = cwd
    elif target.startswith("/"):
        glob_base = target
    else:
        glob_base = ""

    for word, glob_eligible in _classified_tokens(command):
        candidate = word.strip(_SHELL_WORD_EDGE_CHARS)
        if not candidate:
            continue
        if glob_eligible and any(char in candidate for char in _GLOB_ELIGIBLE_CHARS):
            for alternative in _expand_braces(candidate):
                alternative = alternative.strip(_SHELL_WORD_EDGE_CHARS)
                if not alternative:
                    continue
                if _has_glob_chars(alternative):
                    resolved = _resolve_glob(alternative, glob_base)
                    if resolved:
                        globs.add(resolved)
                elif _looks_path_like(alternative):
                    normalized = _normalize_path(alternative, cwd=cwd)
                    if normalized:
                        literals.add(normalized)
            continue
        if not _looks_path_like(candidate):
            continue
        normalized = _normalize_path(candidate, cwd=cwd)
        if normalized:
            literals.add(normalized)

    return literals, globs


def _classified_tokens(command: str) -> list[tuple[str, bool]]:
    """Return ``(word, glob_eligible)`` pairs for a command.

    Words are split the way the shell splits them, with quotes removed and
    backslash escapes applied. A word is glob-eligible only when at least one
    of its metacharacters is unquoted and unescaped: the shell expands nothing
    inside quotes or after a backslash, but ``"/etc/"shado?`` and
    ``/etc/sha\\do?`` still expand, so quoting is tracked per character rather
    than per word. A backslash-newline outside single quotes is a line
    continuation and is removed, so a pattern split across lines is still one
    pattern. Unquoted control characters ``();<>|&`` end a word and are
    emitted on their own, as ``shlex`` does with ``punctuation_chars``.
    """
    tokens: list[tuple[str, bool]] = []
    text: list[str] = []
    active = False
    in_word = False
    quote = ""
    i = 0
    n = len(command)

    def flush() -> None:
        nonlocal text, active, in_word
        if in_word:
            tokens.append(("".join(text), active))
        text = []
        active = False
        in_word = False

    while i < n:
        ch = command[i]
        if quote == "'":
            if ch == "'":
                quote = ""
            else:
                text.append(ch)
        elif quote == '"':
            if ch == '"':
                quote = ""
            elif ch == "\\" and i + 1 < n and command[i + 1] == "\n":
                i += 1  # line continuation: the shell removes both characters
            elif ch == "\\" and i + 1 < n and command[i + 1] in '"\\$`':
                i += 1
                text.append(command[i])
            else:
                text.append(ch)
        elif ch in " \t\r\n":
            flush()
        elif ch in _SHELL_CONTROL_CHARS:
            flush()
            tokens.append((ch, False))
        elif ch in "'\"":
            quote = ch
            in_word = True
        elif ch == "\\":
            if i + 1 < n and command[i + 1] == "\n":
                i += 1  # line continuation: the word continues on the next line
            elif i + 1 < n:
                in_word = True
                i += 1
                text.append(command[i])
            else:
                in_word = True
        else:
            in_word = True
            text.append(ch)
            if ch in _GLOB_ELIGIBLE_CHARS:
                active = True
        i += 1
    flush()
    return tokens


def _expand_braces(value: str) -> list[str]:
    """Expand ``{a,b}`` alternatives, one level, left to right.

    Expansion is bounded: past ``_MAX_BRACE_ALTERNATIVES`` the word is not
    enumerated any further and ``_UNBOUNDED_GLOB`` is returned in its place.
    A pattern the policy cannot enumerate must be treated as one that could
    expand to anything, so the sentinel is a glob that matches every absolute
    root rather than a silently truncated list.
    """
    results = [value]
    while True:
        expanded: list[str] = []
        changed = False
        for item in results:
            match = _BRACE_GROUP.search(item)
            if match is None:
                expanded.append(item)
                continue
            changed = True
            head, tail = item[: match.start()], item[match.end() :]
            expanded.extend(head + alternative + tail for alternative in match.group(1).split(","))
        results = expanded
        if len(results) > _MAX_BRACE_ALTERNATIVES:
            return [_UNBOUNDED_GLOB]
        if not changed:
            return results


def _has_glob_chars(value: str) -> bool:
    return any(char in value for char in _GLOB_MATCH_CHARS)


def _resolve_glob(value: str, base: str) -> str:
    if value.startswith("/"):
        return _collapse_leading_slashes(posixpath.normpath(value))
    if base:
        return _collapse_leading_slashes(posixpath.normpath(posixpath.join(base, value)))
    return ""


def _literal_matches(path: str, sensitive: str) -> bool:
    if sensitive.startswith("/"):
        return path == sensitive or path.startswith(f"{sensitive}/")
    parts = [part for part in path.split("/") if part]
    return sensitive in parts


def _glob_matches(path: str, sensitive: str) -> bool:
    if sensitive.startswith("/"):
        if path == sensitive or path.startswith(f"{sensitive}/"):
            return True
        return _glob_matches_absolute(path, sensitive)
    parts = [part for part in path.split("/") if part]
    if sensitive in parts:
        return True
    return any(_glob_matches_name(part, sensitive) for part in parts)


def _glob_prefix(value: str) -> str:
    """Return the literal text before the first glob metacharacter."""
    index = min(value.index(char) for char in _GLOB_MATCH_CHARS if char in value)
    return value[:index]


def _glob_matches_absolute(path: str, sensitive: str) -> bool:
    """Conservatively decide whether a glob pattern can expand to a sensitive root.

    The executor runs metacharacter commands under ``sh -c``, so a glob is a
    reference to everything it could expand to. Three cases count as a match:
    the glob names something beneath the sensitive root, the glob's literal
    prefix is itself a prefix of the sensitive root (so an expansion could
    land on it), or the glob pattern matches the root outright.
    """
    if not _has_glob_chars(path):
        return False
    prefix = _glob_prefix(path)
    if prefix.startswith(f"{sensitive}/"):
        return True
    if prefix and sensitive.startswith(prefix):
        return True
    return fnmatch.fnmatchcase(sensitive, path)


def _glob_matches_name(part: str, sensitive: str) -> bool:
    """Decide whether one glob path component can expand to a sensitive name."""
    if not _has_glob_chars(part):
        return False
    if sensitive.startswith(".") and not part.startswith("."):
        # Shell globbing never expands a pattern without a leading dot into a
        # dotfile, so ``*`` and ``*.log`` cannot reach ``.ssh``; ``.*`` can.
        return False
    prefix = _glob_prefix(part)
    if prefix and sensitive.startswith(prefix):
        return True
    return fnmatch.fnmatchcase(sensitive, part)


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
