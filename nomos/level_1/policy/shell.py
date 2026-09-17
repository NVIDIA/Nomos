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


# Marks the place where a substitution stood in the outer command. A word in
# command position that carries it names an executable the shell would only
# know after expansion, which policy cannot inspect.
_SUBSTITUTION_MARK = "\x1fSUBST\x1f"
SUBSTITUTED_COMMAND_WORD = "$(...)"


def _split_command_substitutions(command: str) -> tuple[str, list[str]]:
    """Replace every command substitution in ``command`` with a marker.

    Returns the command with each ``$(...)``, ``<(...)`` and ``>(...)`` replaced
    by ``_SUBSTITUTION_MARK``, plus the body of each one in order. The caller
    lexes every body as a command of its own, so a tool named inside a
    substitution is checked like any other command word, and a marker left in
    command position means the executable itself comes from a substitution.

    Quote context follows the shell: single-quoted text is literal and never
    starts a substitution, an apostrophe inside double quotes is literal, and
    double quotes do **not** suppress ``$(...)``. Inside a body the quote state
    starts fresh, and parentheses only count outside quotes, so a quoted ``)``
    does not end the body. A substitution with no closing parenthesis runs to
    the end of the command, which is the conservative reading.

    ``$((...))`` whose text is balanced is arithmetic expansion: it runs no
    command of its own, so only nested ``$(...)`` inside it are extracted.
    """
    outer: list[str] = []
    bodies: list[str] = []
    quote = ""  # "", "'" or '"'
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if ch == "\\" and quote != "'":
            outer.append(command[i : i + 2])
            i += 2
            continue
        if ch == quote:
            quote = ""
            outer.append(ch)
            i += 1
            continue
        if not quote and ch in "'\"":
            quote = ch
            outer.append(ch)
            i += 1
            continue
        # ``$(`` opens a substitution outside quotes and inside double quotes;
        # process substitution only outside quotes; nothing inside single quotes.
        starts = (
            i + 1 < n
            and command[i + 1] == "("
            and ((ch == "$" and quote != "'") or (ch in "<>" and not quote))
        )
        if not starts:
            outer.append(ch)
            i += 1
            continue
        end = _matching_paren(command, i + 1)
        body = command[i + 2 : end]
        if ch == "$" and body.startswith("(") and body.endswith(")") and end < n:
            # Arithmetic: nothing runs directly, but the expression is expanded
            # first, so substitutions nested inside it still do.
            bodies.extend(_split_command_substitutions(body[1:-1])[1])
        else:
            bodies.append(body)
        outer.append(_SUBSTITUTION_MARK)
        i = end + 1
    return "".join(outer), bodies


def _matching_paren(command: str, start: int) -> int:
    """Index of the ``)`` that closes the ``(`` at ``start``, or ``len(command)``.

    Parentheses are counted only outside quotes; quote state starts fresh at
    the opening parenthesis, as it does for the shell. A nested ``$(`` (also
    inside double quotes) opens its own context: it is matched recursively and
    the enclosing quote state resumes after it, so a quote or ``)`` inside the
    nested body cannot end the outer one.
    """
    depth = 0
    quote = ""
    j = start
    n = len(command)
    while j < n:
        c = command[j]
        if c == "\\" and quote != "'":
            j += 2
            continue
        if quote == "'":
            if c == "'":
                quote = ""
            j += 1
            continue
        nested = j + 1 < n and command[j + 1] == "(" and (c == "$" or (c in "<>" and not quote))
        if nested and j > start:
            j = _matching_paren(command, j + 1) + 1
            continue
        if quote:
            if c == quote:
                quote = ""
        elif c in "'\"":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return n


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
            if _SUBSTITUTION_MARK in word:
                # The executable name is produced by a substitution at run
                # time; report it as such so policy fails closed.
                command_words.append(SUBSTITUTED_COMMAND_WORD)
            else:
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
