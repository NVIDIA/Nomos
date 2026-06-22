# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


"""Shell command policy helpers."""

import re
import shlex

_COMMAND_SEPARATORS = {"|", "||", "&", "&&", ";"}
_COMMAND_GROUP_TOKENS = {"(", ")"}
_REDIRECT_TOKENS = {">", ">>", "<", "<<", "<<<", ">|", "<>", "&>", "&>>", "2>", "2>>"}
_SHELL_BUILTINS = {"cd"}
_WRITE_REDIRECT_OPERATORS = (("&>>", "&>"), (">>", ">|", "<>", ">"))


def _is_env_assignment(word: str) -> bool:
    """Return True for shell-style NAME=value assignments before a command."""
    name, sep, _ = word.partition("=")
    return bool(sep and name and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name))


def _shell_tokens(command: str) -> list[str]:
    """Tokenize a shell command while preserving command separators."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;()<>")
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return command.strip().split()


def _shell_tokens_preserving_redirection_words(command: str) -> list[str]:
    """Tokenize shell words while preserving compact redirections like ``2>/dev/null``."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;()")
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return command.strip().split()


def _is_non_persistent_redirect_target(target: str) -> bool:
    """Return True when a redirection target is not a filesystem write."""
    target = target.strip()
    if not target:
        return True
    if target.startswith("&"):
        return True
    return target == "/dev/null"


def _split_write_redirect_token(token: str) -> tuple[str, str] | None:
    """Return (operator, target) for a shell token containing a write redirection."""
    if any(ch.isspace() for ch in token):
        return None

    for operator_group in _WRITE_REDIRECT_OPERATORS:
        for operator in operator_group:
            index = token.find(operator)
            if index < 0:
                continue
            return operator, token[index + len(operator) :]
    return None


def shell_redirection_writes_to_disk(command: str) -> bool:
    """Return True when shell redirection writes to a persistent filesystem target.

    Stderr/stdout suppression to ``/dev/null`` and file descriptor duplication
    such as ``2>&1`` are not treated as disk writes.
    """
    tokens = _shell_tokens_preserving_redirection_words(command)
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in _COMMAND_SEPARATORS or token in _COMMAND_GROUP_TOKENS:
            i += 1
            continue

        split = _split_write_redirect_token(token)
        if split is None:
            i += 1
            continue

        operator, attached_target = split
        target = attached_target
        if not target and i + 1 < len(tokens):
            target = tokens[i + 1]

        if operator in {
            "<>",
            ">",
            ">>",
            ">|",
            "&>",
            "&>>",
        } and not _is_non_persistent_redirect_target(target):
            return True
        i += 1

    return False


def extract_shell_command_words(command: str) -> list[str]:
    """Extract command words from each segment of a shell command."""
    command_words: list[str] = []
    expect_command = True
    skip_redirect_target = False

    for word in _shell_tokens(command):
        if not word:
            continue

        if word in _COMMAND_SEPARATORS:
            expect_command = True
            skip_redirect_target = False
            continue

        if word in _COMMAND_GROUP_TOKENS:
            if word == "(":
                expect_command = True
            continue

        if word in _REDIRECT_TOKENS or set(word) <= {">", "<"}:
            skip_redirect_target = True
            continue

        if skip_redirect_target:
            skip_redirect_target = False
            continue

        if expect_command:
            if _is_env_assignment(word):
                continue
            command_words.append(word)
            expect_command = False

    return command_words


def shell_path_policy_error(command: str) -> str:
    """Require shell commands to invoke executables by absolute path."""
    bare_commands = [
        word
        for word in extract_shell_command_words(command)
        if not word.startswith("/") and word not in _SHELL_BUILTINS
    ]
    if not bare_commands:
        return ""

    joined = ", ".join(dict.fromkeys(bare_commands))
    return (
        "Shell commands must invoke executables by absolute path; "
        f"non-absolute command word(s): {joined}"
    )
