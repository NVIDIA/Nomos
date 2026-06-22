# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


from __future__ import annotations

import hashlib
import json
from pathlib import Path

from monitor.server.ledger import get_ledger_entry, load_ledger_page, load_ledger_snapshot


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


def test_load_ledger_snapshot_summarizes_and_redacts(tmp_path):
    ledger = tmp_path / "council-ledger.jsonl"
    _genesis, request = _write_ledger(ledger)

    snapshot = load_ledger_snapshot([str(ledger)])

    assert snapshot["summary"]["chain_count"] == 1
    assert snapshot["summary"]["entry_count"] == 2
    assert snapshot["summary"]["valid_chain_count"] == 1
    assert snapshot["summary"]["signed_entry_count"] == 2
    assert snapshot["chains"][0]["chain_id"] == "ledger-council-test"
    assert snapshot["chains"][0]["is_valid"] is True

    rows = snapshot["entries"]
    submitted = next(row for row in rows if row["sequence"] == 1)
    assert submitted["intent_id"] == "intent-1"
    assert submitted["agent_id"] == "agent-test"
    assert submitted["content_hash"] == request["content_hash"]
    assert submitted["hash_valid"] is True
    assert submitted["link_valid"] is True

    detail = get_ledger_entry(chain_id="ledger-council-test", sequence=1, patterns=[str(ledger)])
    assert detail is not None
    assert detail["payload"]["auth_token"] == "[redacted]"
    assert detail["metadata"]["auth_token"] == "[redacted]"


def test_load_ledger_snapshot_flags_broken_chain(tmp_path):
    ledger = tmp_path / "broken-ledger.jsonl"
    genesis, request = _write_ledger(ledger)
    request["previous_hash"] = "sha256:broken"
    ledger.write_text(json.dumps(genesis) + "\n" + json.dumps(request) + "\n", encoding="utf-8")

    snapshot = load_ledger_snapshot([str(ledger)])

    assert snapshot["summary"]["invalid_chain_count"] == 1
    assert snapshot["chains"][0]["is_valid"] is False
    assert snapshot["chains"][0]["broken_at"] == 1
    broken = next(row for row in snapshot["entries"] if row["sequence"] == 1)
    assert broken["link_valid"] is False


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
