# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Permission model and intent categorization.

Provides a deterministic (non-LLM) pre-filter that maps intents
to required permission scopes based on command heuristics.
"""

import re
import shlex
from enum import StrEnum

from nomos.level_0.types import Intent
from nomos.level_1.policy.shell import shell_redirection_writes_to_disk


class PermissionScope(StrEnum):
    """Permission scopes for agent actions."""

    SHELL_READ = "shell:read"  # Non-mutating shell commands (ls, cat, ps, etc.)
    SHELL_WRITE = "shell:write"  # Mutating shell commands (mkdir, cp, mv, etc.)
    FILE_READ = "file:read"  # Read file contents
    FILE_WRITE = "file:write"  # Create/modify files
    FILE_DELETE = "file:delete"  # Delete files
    INFRA_DEPLOY = "infra:deploy"  # Deploy infrastructure
    INFRA_DESTROY = "infra:destroy"  # Destroy infrastructure


# ── Command classification patterns ──────────────────────────────────

# Commands that are strictly read-only
_READ_ONLY_COMMANDS = frozenset(
    {
        "ls",
        "cat",
        "head",
        "tail",
        "wc",
        "grep",
        "egrep",
        "fgrep",
        "find",
        "locate",
        "which",
        "whereis",
        "type",
        "file",
        "date",
        "uptime",
        "whoami",
        "id",
        "hostname",
        "uname",
        "ps",
        "top",
        "htop",
        "free",
        "df",
        "du",
        "mount",
        "env",
        "printenv",
        "echo",
        "printf",
        "true",
        "false",
        "test",
        "stat",
        "readlink",
        "realpath",
        "basename",
        "dirname",
        "pwd",
        "groups",
        "lsof",
        "netstat",
        "ss",
        "ip",
        "ifconfig",
        "dig",
        "nslookup",
        "host",
        "ping",
        "traceroute",
        "git log",
        "git status",
        "git diff",
        "git show",
        "git branch",
    }
)

# Commands that write/modify files
_FILE_WRITE_COMMANDS = frozenset(
    {
        "touch",
        "tee",
        "dd",
        "truncate",
        "sed",
        "awk",  # when used with -i
    }
)

# Commands that delete files
_FILE_DELETE_COMMANDS = frozenset(
    {
        "rm",
        "rmdir",
        "unlink",
        "shred",
    }
)

# Commands that move/copy (write)
_SHELL_WRITE_COMMANDS = frozenset(
    {
        "mkdir",
        "cp",
        "mv",
        "ln",
        "install",
        "chmod",
        "chown",
        "chgrp",
        "tar",
        "zip",
        "unzip",
        "gzip",
        "gunzip",
        "bzip2",
        "git add",
        "git commit",
        "git push",
        "git merge",
        "git rebase",
        "git checkout",
        "git reset",
        "git stash",
        "pip",
        "pip3",
        "npm",
        "yarn",
        "apt",
        "apt-get",
        "yum",
        "dnf",
    }
)

# Infrastructure deploy commands
_INFRA_DEPLOY_PATTERNS = [
    re.compile(r"\bkubectl\s+(apply|create|scale|rollout|patch)\b"),
    re.compile(r"\bterraform\s+(apply|plan|init)\b"),
    re.compile(r"\bansible-playbook\b"),
    re.compile(r"\bhelm\s+(install|upgrade)\b"),
    re.compile(r"\bdocker\s+(run|build|push|compose\s+up)\b"),
]

# Infrastructure destroy commands
_INFRA_DESTROY_PATTERNS = [
    re.compile(r"\bkubectl\s+delete\b"),
    re.compile(r"\bterraform\s+destroy\b"),
    re.compile(r"\bhelm\s+(uninstall|delete)\b"),
    re.compile(r"\bdocker\s+(rm|rmi|stop|kill|compose\s+down)\b"),
]

# Redirect/pipe patterns that indicate file write
_FILE_WRITE_PATTERNS = [
    re.compile(r"\btee\b"),
]


def _extract_base_command(command: str) -> str:
    """
    Extract the first command token from a potentially complex command string.

    Parameters
    ----------
    command : str
        The full command string to extract from.

    Returns
    -------
    str
        The base command token.
    """
    # Strip leading env vars, sudo, etc.
    cmd = command.strip()
    for prefix in ("sudo ", "env ", "nohup ", "nice ", "time "):
        if cmd.startswith(prefix):
            cmd = cmd[len(prefix) :].lstrip()

    # Get first token
    try:
        tokens = shlex.split(cmd)
        if tokens:
            return tokens[0].rsplit("/", 1)[-1]
    except ValueError:
        pass

    # Fallback: split on whitespace
    parts = cmd.split()
    return parts[0].rsplit("/", 1)[-1] if parts else ""


def categorize_intent(intent: Intent) -> set[PermissionScope]:
    """
    Deterministic pre-filter: map an intent to required permissions.

    Uses heuristics on the command string. Conservative: if unsure,
    requires the broadest applicable permission set.

    This is NOT a security boundary — it's a fast pre-check to avoid
    wasting LLM calls on requests the agent can't make.

    Parameters
    ----------
    intent : Intent
        The intent to categorize.

    Returns
    -------
    Set[PermissionScope]
        The set of permission scopes required for the intent.
    """
    command = intent.command or (intent.parameters.get("command", "") if intent.parameters else "")
    action_type = intent.action_type
    perms: set[PermissionScope] = set()

    if not command:
        # No command — infer from action_type
        return _categorize_by_action_type(action_type)

    cmd_lower = command.lower().strip()
    base_cmd = _extract_base_command(command).lower()

    # Check infrastructure patterns first (most specific)
    for pattern in _INFRA_DEPLOY_PATTERNS:
        if pattern.search(cmd_lower):
            perms.add(PermissionScope.INFRA_DEPLOY)

    for pattern in _INFRA_DESTROY_PATTERNS:
        if pattern.search(cmd_lower):
            perms.add(PermissionScope.INFRA_DESTROY)

    # Check file delete
    if base_cmd in _FILE_DELETE_COMMANDS or "rm " in cmd_lower or "rm\t" in cmd_lower:
        perms.add(PermissionScope.SHELL_WRITE)
        perms.add(PermissionScope.FILE_DELETE)

    # Check file write (redirects, tee, etc.)
    if shell_redirection_writes_to_disk(command):
        perms.add(PermissionScope.FILE_WRITE)

    for pattern in _FILE_WRITE_PATTERNS:
        if pattern.search(command):
            perms.add(PermissionScope.FILE_WRITE)

    if base_cmd in _FILE_WRITE_COMMANDS:
        perms.add(PermissionScope.FILE_WRITE)

    # Check shell write
    if base_cmd in _SHELL_WRITE_COMMANDS:
        perms.add(PermissionScope.SHELL_WRITE)

    # Check read-only
    if base_cmd in _READ_ONLY_COMMANDS and not perms:
        perms.add(PermissionScope.SHELL_READ)

    # If we still have nothing, be conservative
    if not perms:
        perms.add(PermissionScope.SHELL_READ)
        perms.add(PermissionScope.SHELL_WRITE)

    # If command reads files explicitly (cat, head, tail, etc.)
    if base_cmd in {"cat", "head", "tail", "less", "more", "grep", "find"}:
        perms.add(PermissionScope.FILE_READ)

    return perms


def _categorize_by_action_type(action_type: str) -> set[PermissionScope]:
    """
    Fallback categorization based on action_type string.

    Parameters
    ----------
    action_type : str
        The action type string to categorize.

    Returns
    -------
    Set[PermissionScope]
        The set of permission scopes derived from the action type.
    """
    at = action_type.lower()

    if "delete" in at or "destroy" in at or "remove" in at:
        return {PermissionScope.SHELL_WRITE, PermissionScope.FILE_DELETE}
    if "write" in at or "create" in at or "modify" in at:
        return {PermissionScope.SHELL_WRITE, PermissionScope.FILE_WRITE}
    if "deploy" in at:
        return {PermissionScope.INFRA_DEPLOY}
    if "read" in at or "info" in at or "list" in at or "get" in at:
        return {PermissionScope.SHELL_READ, PermissionScope.FILE_READ}

    # Unknown — require broad permissions
    return {PermissionScope.SHELL_READ, PermissionScope.SHELL_WRITE}
