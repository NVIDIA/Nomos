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

_COMMAND_SEPARATORS = {"|", "||", "&", "&&", ";", "\n"}
_LINE_BREAK = re.compile(r"\r\n|\r|\n")
_COMMAND_GROUP_TOKENS = {"(", ")"}
_REDIRECT_TOKENS = {">", ">>", "<", "<<", "<<<", ">|", "<>", "&>", "&>>", "2>", "2>>"}
_SHELL_BUILTINS = {"cd"}
_WRITE_REDIRECT_OPERATORS = (("&>>", "&>"), (">>", ">|", "<>", ">"))


def _is_env_assignment(word: str) -> bool:
    """Return True for shell-style NAME=value assignments before a command."""
    name, sep, _ = word.partition("=")
    return bool(sep and name and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name))


def _lex_lines(command: str, punctuation_chars: str) -> list[str]:
    """Tokenize each line separately and join them with an explicit ``\\n`` separator.

    ``shlex`` treats a line break as whitespace, but ``sh -c`` treats it as a
    command terminator. Splitting first keeps the two views in agreement.

    This is conservative tokenization, not shell-equivalent parsing: a line
    break inside quotes and a backslash-newline continuation are split as well,
    and a bare carriage return is treated as a separator although POSIX ``sh``
    does not. Commands containing any line break are rejected at intake and at
    the executor, so the split only matters as defense in depth.
    """
    tokens: list[str] = []
    for index, line in enumerate(_LINE_BREAK.split(command)):
        if index:
            tokens.append("\n")
        try:
            lexer = shlex.shlex(line, posix=True, punctuation_chars=punctuation_chars)
            lexer.whitespace_split = True
            tokens.extend(lexer)
        except ValueError:
            tokens.extend(line.strip().split())
    return tokens


def _split_command_substitutions(command: str) -> tuple[str, list[str]]:
    """Cut every ``$(...)`` command substitution out of ``command``.

    Returns the command with each substitution removed, plus the body of each
    one in order. The caller lexes every body as a command of its own, so a
    tool named inside a substitution is checked like any other command word.

    Single-quoted text is literal to the shell and is left alone. Double quotes
    do **not** suppress substitution, so bodies inside them are cut out too. A
    ``$(`` with no closing parenthesis is taken to run to the end of the
    command, which is the conservative reading. ``$((...))`` yields a body
    wrapped in parentheses: bash falls back to a subshell substitution when the
    text is not arithmetic, so the body is lexed as a command group. Nested
    substitutions are found when the body is lexed.
    """
    outer: list[str] = []
    bodies: list[str] = []
    in_single = False
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if ch == "\\" and not in_single:
            outer.append(command[i : i + 2])
            i += 2
            continue
        if ch == "'":
            in_single = not in_single
        if in_single or ch != "$" or i + 1 >= n or command[i + 1] != "(":
            outer.append(ch)
            i += 1
            continue
        depth = 0
        body_single = False
        j = i + 1
        while j < n:
            c = command[j]
            if c == "\\" and not body_single:
                j += 2
                continue
            if c == "'":
                body_single = not body_single
            elif not body_single:
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        break
            j += 1
        bodies.append(command[i + 2 : j])
        i = j + 1
    return "".join(outer), bodies


def _shell_tokens(command: str) -> list[str]:
    """Tokenize a shell command while preserving command separators."""
    return _lex_lines(command, "|&;()<>")


def _shell_tokens_preserving_redirection_words(command: str) -> list[str]:
    """Tokenize shell words while preserving compact redirections like ``2>/dev/null``."""
    return _lex_lines(command, "|&;()")


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

    outer, substitutions = _split_command_substitutions(command)

    for word in _shell_tokens(outer):
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

    # ``$(...)`` runs a command of its own, whether or not it sits inside double
    # quotes, so its body is lexed as a further segment.
    for body in substitutions:
        command_words.extend(extract_shell_command_words(body))

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
