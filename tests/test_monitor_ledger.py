# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from monitor.server.ledger import get_ledger_entry, load_ledger_page


def _entry(
    *,
    sequence: int,
    entry_type: str,
    payload: dict,
    previous_hash: str | None,
    signer_id: str = "council-key",
) -> dict:
    timestamp = f"2026-05-01T12:00:0{sequence}+00:00"
    content = {
        "sequence": sequence,
        "entry_type": entry_type,
        "payload": payload,
        "timestamp": timestamp,
        "previous_hash": previous_hash,
    }
    digest = hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        **content,
        "content_hash": f"sha256:{digest}",
        "signatures": [{"signer_id": signer_id, "signature_hex": "abc123", "algorithm": "ed25519"}],
        "metadata": {"auth_token": "do-not-leak"},
    }


def _write_ledger(path: Path) -> tuple[dict, dict]:
    genesis = _entry(
        sequence=0,
        entry_type="policy_updated",
        payload={"chain_id": "ledger-council-test", "creator_id": "test"},
        previous_hash=None,
    )
    request = _entry(
        sequence=1,
        entry_type="intent_submitted",
        payload={
            "intent_id": "intent-1",
            "agent_id": "agent-test",
            "command": "/bin/echo hi",
            "auth_token": "raw-token",
        },
        previous_hash=genesis["content_hash"],
    )
    path.write_text(json.dumps(genesis) + "\n" + json.dumps(request) + "\n", encoding="utf-8")
    return genesis, request


def _write_many(path: Path, count: int) -> list[dict]:
    entries: list[dict] = []
    previous_hash = None
    for sequence in range(count):
        entry = _entry(
            sequence=sequence,
            entry_type="policy_updated" if sequence == 0 else "intent_submitted",
            payload={
                "chain_id": "ledger-council-test",
                "intent_id": f"intent-{sequence}",
            },
            previous_hash=previous_hash,
        )
        entries.append(entry)
        previous_hash = entry["content_hash"]
    path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8")
    return entries


def test_get_ledger_entry_redacts_secrets(tmp_path):
    ledger = tmp_path / "council-ledger.jsonl"
    _write_ledger(ledger)

    detail = get_ledger_entry(chain_id="ledger-council-test", sequence=1, patterns=[str(ledger)])
    assert detail is not None
    assert detail["payload"]["auth_token"] == "[redacted]"
    assert detail["metadata"]["auth_token"] == "[redacted]"


def test_load_ledger_page_validate_flags_broken_chain(tmp_path):
    ledger = tmp_path / "broken-ledger.jsonl"
    genesis, request = _write_ledger(ledger)
    request["previous_hash"] = "sha256:broken"
    ledger.write_text(json.dumps(genesis) + "\n" + json.dumps(request) + "\n", encoding="utf-8")

    page = load_ledger_page(patterns=[str(ledger)], validate=True)

    assert page["summary"]["invalid_chain_count"] == 1
    assert page["chains"][0]["is_valid"] is False
    assert page["chains"][0]["broken_at"] == 1

    # The tampered entry must still be identifiable row-by-row, not only through
    # the chain summary: recomputing its content hash is what catches the edit.
    broken = next(row for row in page["entries"] if row["sequence"] == 1)
    assert broken["hash_valid"] is False
    assert page["entries"][-1]["hash_valid"] is True


def test_file_entry_cannot_self_certify_with_projection_flags(tmp_path):
    """Projection verdicts are trusted only from the council API reader.

    A JSONL record that carries ``projected``/``hash_valid`` must still have
    its hash recomputed: otherwise an edited file entry could mark itself valid
    and vanish from the default (fast) integrity view.
    """
    ledger = tmp_path / "self-certified.jsonl"
    genesis, request = _write_ledger(ledger)
    request["payload"]["command"] = "/bin/echo forged"
    request["projected"] = True
    request["hash_valid"] = True
    request["projected_fields"] = ["payload.intent.evidence"]
    ledger.write_text(json.dumps(genesis) + "\n" + json.dumps(request) + "\n", encoding="utf-8")

    fast = load_ledger_page(patterns=[str(ledger)])
    assert fast["summary"]["invalid_chain_count"] == 1
    assert fast["chains"][0]["broken_at"] == 1
    forged = next(row for row in fast["entries"] if row["sequence"] == 1)
    assert forged["hash_valid"] is False

    full = load_ledger_page(patterns=[str(ledger)], validate=True)
    assert full["summary"]["invalid_chain_count"] == 1
    assert next(row for row in full["entries"] if row["sequence"] == 1)["hash_valid"] is False

    detail = get_ledger_entry(patterns=[str(ledger)], chain_id="ledger-council-test", sequence=1)
    assert detail is not None
    assert detail["hash_valid"] is False
    assert detail["projected"] is False


def test_load_ledger_page_returns_cursor_and_next_page(tmp_path):
    ledger = tmp_path / "paged-ledger.jsonl"
    _entries = _write_many(ledger, 5)

    first = load_ledger_page(patterns=[str(ledger)], limit=2)
    second = load_ledger_page(patterns=[str(ledger)], limit=2, cursor=first["next_cursor"])

    assert [row["sequence"] for row in first["entries"]] == [4, 3]
    assert first["has_more"] is True
    assert first["summary"]["entry_count"] == 5
    assert [row["sequence"] for row in second["entries"]] == [2, 1]
    assert second["has_more"] is True


def test_load_ledger_page_filters_server_side(tmp_path):
    ledger = tmp_path / "filtered-ledger.jsonl"
    _entries = _write_many(ledger, 6)

    page = load_ledger_page(patterns=[str(ledger)], limit=2, query="intent-1")

    assert [row["intent_id"] for row in page["entries"]] == ["intent-1"]
