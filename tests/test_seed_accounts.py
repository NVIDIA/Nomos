# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Regression cover for scripts/seed_accounts.py summary rendering."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "seed_accounts.py"
SPEC = importlib.util.spec_from_file_location("seed_accounts", SCRIPT)
seed_accounts = importlib.util.module_from_spec(SPEC)
assert SPEC is not None
assert SPEC.loader is not None
sys.modules[SPEC.name] = seed_accounts
SPEC.loader.exec_module(seed_accounts)


def test_print_summary_renders_the_record_provisioning_actually_returns(capsys):
    """Seeding must not crash after the tokens have been written.

    print_summary read `token_hash`, but provisioning.create_account returns
    `token_hash_prefix`. The KeyError fired *after* the accounts were persisted,
    while the raw tokens existed only in memory -- so every successful seed run
    destroyed the only copy of the credentials it had just minted.
    """
    record = {
        "agent_id": "agent-developer",
        "display_name": "Agent Developer",
        "token": "f" * 64,  # pragma: allowlist secret
        "token_hash_prefix": "abc123def4567890...",  # pragma: allowlist secret
        "permissions": ["shell:read", "shell:write"],
        "councils": ["council-prod"],
    }

    seed_accounts.print_summary([record])

    out = capsys.readouterr().out
    assert "agent-developer" in out
    assert "abc123def4567890" in out  # pragma: allowlist secret
