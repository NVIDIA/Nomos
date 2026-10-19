# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

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


def test_spliced_genesis_breaks_the_file_chain(tmp_path):
    """Genesis is checked against its successor's previous_hash too."""
    ledger = tmp_path / "spliced.jsonl"
    _genesis, request = _write_ledger(ledger)
    other = _entry(
        sequence=0,
        entry_type="policy_updated",
        payload={"chain_id": "ledger-council-test", "creator_id": "someone-else"},
        previous_hash=None,
    )
    ledger.write_text(json.dumps(other) + "\n" + json.dumps(request) + "\n", encoding="utf-8")

    page = load_ledger_page(patterns=[str(ledger)], validate=True)
    assert page["summary"]["invalid_chain_count"] == 1
    # Full validation walks oldest-first and reports the first bad edge; the
    # genesis row itself is also marked unlinked from its successor.
    assert page["chains"][0]["broken_at"] in (0, 1)
    genesis_row = next(row for row in page["entries"] if row["sequence"] == 0)
    assert genesis_row["hash_valid"] is True
    assert genesis_row["link_valid"] is False


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


# ── signature verification ─────────────────────────────────────────────────


def _keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    return private, private.public_key().public_bytes_raw()


def _sign_entry(entry: dict, private) -> dict:
    digest = bytes.fromhex(entry["content_hash"].split(":", 1)[1])
    entry["signatures"] = [
        {
            "signer_id": "council-key",
            "signature_hex": private.sign(digest).hex(),
            "algorithm": "ed25519",
        }
    ]
    return entry


def _write_signed_ledger(path: Path, private, count: int = 3) -> list[dict]:
    entries: list[dict] = []
    previous_hash = None
    for sequence in range(count):
        entry = _entry(
            sequence=sequence,
            entry_type="policy_updated" if sequence == 0 else "intent_submitted",
            payload={"chain_id": "ledger-council-test", "writ_id": f"w-{sequence}"},
            previous_hash=previous_hash,
        )
        _sign_entry(entry, private)
        entries.append(entry)
        previous_hash = entry["content_hash"]
    path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8")
    return entries


def _rehash(entry: dict) -> None:
    content = {
        key: entry[key]
        for key in ("sequence", "entry_type", "payload", "timestamp", "previous_hash")
    }
    digest = hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    entry["content_hash"] = f"sha256:{digest}"


def test_signatures_verify_against_trusted_key(tmp_path):
    private, public = _keypair()
    ledger = tmp_path / "signed.jsonl"
    _write_signed_ledger(ledger, private)

    snapshot = load_ledger_snapshot([str(ledger)], public_keys=[public])
    assert snapshot["chains"][0]["is_valid"] is True
    assert snapshot["chains"][0]["signatures_verified"] is True
    assert all(row["signature_valid"] is True for row in snapshot["entries"])

    page = load_ledger_page(patterns=[str(ledger)], validate=True, public_keys=[public])
    assert page["chains"][0]["signatures_verified"] is True
    assert all(row["signature_valid"] is True for row in page["entries"])

    detail = get_ledger_entry(
        chain_id="ledger-council-test", sequence=1, patterns=[str(ledger)], public_keys=[public]
    )
    assert detail is not None and detail["signature_valid"] is True


def test_resigned_rewrite_is_flagged_only_when_keys_are_known(tmp_path):
    """Rewriting entry 1 and re-threading every hash defeats the hash check;
    only the signatures catch it, and only when the monitor holds the key."""
    private, public = _keypair()
    ledger = tmp_path / "rewritten.jsonl"
    entries = _write_signed_ledger(ledger, private)
    entries[1]["payload"]["writ_id"] = "w-FORGED"
    _rehash(entries[1])
    entries[2]["previous_hash"] = entries[1]["content_hash"]
    _rehash(entries[2])
    ledger.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")

    unaware = load_ledger_snapshot([str(ledger)])
    assert unaware["chains"][0]["is_valid"] is True
    assert unaware["chains"][0]["signatures_verified"] is False
    assert all(row["signature_valid"] is None for row in unaware["entries"])

    aware = load_ledger_snapshot([str(ledger)], public_keys=[public])
    assert aware["chains"][0]["is_valid"] is False
    assert aware["chains"][0]["broken_at"] == 1
    assert aware["summary"]["invalid_chain_count"] == 1
    forged = next(row for row in aware["entries"] if row["sequence"] == 1)
    assert forged["hash_valid"] is True and forged["link_valid"] is True
    assert forged["signature_valid"] is False
    broken_rows = load_ledger_page(
        patterns=[str(ledger)], integrity="broken", public_keys=[public]
    )["entries"]
    assert sorted(row["sequence"] for row in broken_rows) == [1, 2]


def test_wrong_key_and_unsigned_entries_fail_verification(tmp_path):
    private, _public = _keypair()
    _other, wrong_public = _keypair()
    ledger = tmp_path / "wrong-key.jsonl"
    entries = _write_signed_ledger(ledger, private)

    snapshot = load_ledger_snapshot([str(ledger)], public_keys=[wrong_public])
    assert snapshot["chains"][0]["is_valid"] is False
    assert snapshot["chains"][0]["broken_at"] == 0

    entries[2]["signatures"] = []
    ledger.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    _private2, public2 = private, private.public_key().public_bytes_raw()
    snapshot = load_ledger_snapshot([str(ledger)], public_keys=[public2])
    assert snapshot["chains"][0]["broken_at"] == 2


def test_load_trusted_public_keys_parses_hex_and_files(tmp_path):
    from monitor.server.ledger import load_trusted_public_keys

    _private, public = _keypair()
    raw_file = tmp_path / "council.pub"
    raw_file.write_bytes(public)
    hex_file = tmp_path / "council.hex"
    hex_file.write_text(public.hex() + "\n")
    raw_newline_file = tmp_path / "council-newline.pub"
    raw_newline_file.write_bytes(public + b"\n")  # shell redirection appends one

    keys = load_trusted_public_keys(
        [
            f"hex:{public.hex()}",
            str(raw_file),
            str(hex_file),
            str(raw_newline_file),
            str(tmp_path / "missing.pub"),
            "hex:notahexstring",
            "",
        ]
    )
    assert keys == [public, public, public, public]


def test_raw_key_whitespace_bytes_survive_newline_trim(tmp_path):
    """A key whose first or last byte is ASCII whitespace must load intact when
    the file carries an appended line ending."""
    from monitor.server.ledger import load_trusted_public_keys

    key = bytes([0x0C]) + b"k" * 30 + bytes([0x0B])  # form feed ... vertical tab
    cr_key = b"k" * 31 + bytes([0x0D])  # a key whose last byte is CR
    lf = tmp_path / "lf.pub"
    lf.write_bytes(key + b"\n")
    crlf = tmp_path / "crlf.pub"
    crlf.write_bytes(key + b"\r\n")
    exact = tmp_path / "exact.pub"
    exact.write_bytes(key)
    cr_lf = tmp_path / "cr-key-lf.pub"
    cr_lf.write_bytes(cr_key + b"\n")  # last two bytes look like CRLF but are not

    assert load_trusted_public_keys([str(lf), str(crlf), str(exact), str(cr_lf)]) == [
        key,
        key,
        key,
        cr_key,
    ]


def test_fast_summary_flags_a_head_whose_signature_fails(tmp_path):
    """Fast mode inspects only the head; a re-hashed head with a stale
    signature must count as a broken chain, not a valid one."""
    private, public = _keypair()
    ledger = tmp_path / "forged-head.jsonl"
    entries = _write_signed_ledger(ledger, private)
    entries[2]["payload"]["writ_id"] = "w-FORGED"
    _rehash(entries[2])
    ledger.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")

    unaware = load_ledger_page(patterns=[str(ledger)])
    assert unaware["chains"][0]["is_valid"] is True

    aware = load_ledger_page(patterns=[str(ledger)], public_keys=[public])
    assert aware["validation_mode"] == "fast"
    assert aware["chains"][0]["is_valid"] is False
    assert aware["chains"][0]["broken_at"] == 2
    assert aware["summary"]["invalid_chain_count"] == 1
