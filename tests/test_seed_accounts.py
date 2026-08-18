# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Regression cover for scripts/seed_accounts.py.

These tests deliberately drive `seed()` rather than constructing its output by
hand. An earlier version of this file built the summary record itself, which
made it pass while the real path still raised KeyError: the record `seed()`
actually produces did not carry the key `print_summary` reads. A test that
supplies its own input cannot catch a producer/consumer mismatch.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "seed_accounts.py"
SPEC = importlib.util.spec_from_file_location("seed_accounts", SCRIPT)
seed_accounts = importlib.util.module_from_spec(SPEC)
assert SPEC is not None
assert SPEC.loader is not None
sys.modules[SPEC.name] = seed_accounts
SPEC.loader.exec_module(seed_accounts)


class _FakeStore:
    """Stands in for RedisStore so seed() can run without a live Redis."""

    _account_integrity_key = "k" * 32

    async def connect(self) -> None: ...

    async def close(self) -> None: ...


def _run_seed(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Execute the real seed() with only the store and provisioning stubbed."""
    created: list[dict] = []

    async def fake_create_account(_store, **kwargs):
        record = {
            "agent_id": kwargs["agent_id"],
            "display_name": kwargs["display_name"],
            "enabled": True,
            "role": "",
            "token": "f" * 64,  # pragma: allowlist secret
            "token_hash_prefix": "abc123def4567890...",  # pragma: allowlist secret
            "permissions": list(kwargs["permissions"]),
            "councils": list(kwargs["councils"]),
            "created_at": "0",
            "updated_at": "0",
        }
        created.append(record)
        return record

    from nomos.auth import provisioning

    monkeypatch.setattr(provisioning, "create_account", fake_create_account)
    monkeypatch.setattr(provisioning, "require_signed_store", lambda *a, **k: "")
    monkeypatch.setattr("nomos.store.redis_store.RedisStore", lambda _config: _FakeStore())

    return asyncio.run(seed_accounts.seed("redis://localhost:6379/0", "nomos:"))


def test_seed_records_carry_every_field_the_summary_renders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The consumer must not read a key the producer does not emit.

    print_summary rendered `token_hash`, then `token_hash_prefix`; seed() emitted
    neither. Both raised KeyError *after* the accounts were written, while the raw
    tokens existed only in memory — so a successful seed destroyed the only copy
    of the credentials it had just minted.
    """
    results = _run_seed(monkeypatch)

    assert results, "seed() produced no records"

    source = SCRIPT.read_text(encoding="utf-8")
    body = source.split("def print_summary", 1)[1]
    rendered_keys = set(__import__("re").findall(r"r\[[\"']([^\"']+)[\"']\]", body))
    assert rendered_keys, "could not determine which keys print_summary renders"

    for record in results:
        missing = rendered_keys - record.keys()
        assert not missing, f"print_summary renders keys seed() never emits: {missing}"


def test_print_summary_runs_against_a_real_seed_record(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end: whatever seed() emits must survive print_summary."""
    results = _run_seed(monkeypatch)

    seed_accounts.print_summary(results)

    out = capsys.readouterr().out
    assert "agent-developer" in out
    assert "abc123def4567890" in out  # pragma: allowlist secret
