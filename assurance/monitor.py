# SPDX-License-Identifier: Apache-2.0
"""Verified ingestion and durable, separate ingestion/analysis checkpoints."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import httpx

from nomos.level_0.crypto import ContentHasher, Verifier
from nomos.level_1.ledger.entry import LedgerEntry


class IntegrityError(ValueError):
    """Source data failed the observer's trust contract."""


def verify_batch(
    rows: list[dict], sequence: int, previous: str | None, chain_id: str, keys: dict[str, str]
) -> list[dict]:
    verifier = Verifier()
    for key_id, key in keys.items():
        verifier.register_public_key(key_id, bytes.fromhex(key))
    clean = []
    for row in rows:
        try:
            entry = LedgerEntry.from_dict(row)
            if type(entry.sequence) is not int or entry.sequence != sequence + 1:
                raise IntegrityError("non-contiguous sequence")
            if entry.timestamp.tzinfo is None:
                raise IntegrityError("timestamp lacks timezone")
            if row.get("previous_hash") != previous:
                raise IntegrityError("previous hash mismatch")
            if entry.sequence == 0 and (
                previous is not None or entry.payload.get("chain_id") != chain_id
            ):
                raise IntegrityError("unexpected genesis chain")
            expected = ContentHasher().hash_json(entry.content_for_hashing())
            if entry.content_hash != expected:
                raise IntegrityError("content hash mismatch")
            if not entry.signatures or any(
                sig.algorithm != "ed25519" or not verifier.verify(expected.to_bytes(), sig)
                for sig in entry.signatures
            ):
                raise IntegrityError("missing or untrusted signature")
            normalized = entry.to_dict()
            # Nomos metadata is NOT covered by the entry signature.
            normalized.pop("metadata", None)
            clean.append(normalized)
            sequence, previous = entry.sequence, str(expected)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise IntegrityError(f"invalid entry at expected sequence {sequence + 1}") from exc
    return clean


class Source(Protocol):
    async def page(self, cursor: int | None = None, limit: int = 100) -> dict: ...

    async def get(self, sequence: int) -> dict: ...


class LedgerSource:
    """Existing council debug-tool contract, with bounded authenticated reads."""

    def __init__(self, base_url: str, council_id: str, token: str, *, client: httpx.AsyncClient):
        self.base_url = base_url.rstrip("/")
        self.council_id = council_id
        self.token = token
        self.client = client

    async def call(self, tool: str, fields: dict) -> dict:
        async with self.client.stream(
            "POST",
            f"{self.base_url}/debug/tools/call/{tool}",
            headers={"Authorization": f"Bearer {self.token}"},
            json={"council_id": self.council_id, **fields},
            timeout=15,
        ) as response:
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > 8 * 1024 * 1024:
                    raise IntegrityError("source response exceeds 8 MiB limit")
        result = json.loads(data)
        if isinstance(result, dict) and "result" in result:
            result = result["result"]
        if isinstance(result, str):
            result = json.loads(result)
        if not isinstance(result, dict) or result.get("error"):
            raise IntegrityError("source returned an invalid or unsuccessful response")
        return result

    async def page(self, cursor: int | None = None, limit: int = 100) -> dict:
        return await self.call("council_ledger_page", {"cursor": cursor, "limit": limit})

    async def get(self, sequence: int) -> dict:
        result = await self.call("council_ledger_get", {"sequence": sequence})
        if not result.get("found") or not isinstance(result.get("entry"), dict):
            raise IntegrityError("checkpoint entry missing from source")
        return result["entry"]


class Store:
    """Single-writer SQLite on a PVC. Events and ingestion cursor commit together."""

    def __init__(self, path: Path, chain_id: str):
        self.path, self.chain_id = path, chain_id
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY, hash TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS findings (id TEXT PRIMARY KEY, body TEXT NOT NULL);
            """)
            saved = db.execute("SELECT value FROM settings WHERE key='chain_id'").fetchone()
            if saved and saved[0] != chain_id:
                raise ValueError("state belongs to a different chain")
            db.execute("INSERT OR IGNORE INTO settings VALUES ('chain_id', ?)", (chain_id,))
        path.chmod(0o600)

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def checkpoint(self) -> tuple[int, str | None]:
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT sequence,hash FROM events ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
        return tuple(row) if row else (-1, None)

    def append(self, rows: list[dict], expected: tuple[int, str | None]) -> None:
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT sequence,hash FROM events ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            if (tuple(row) if row else (-1, None)) != expected:
                raise IntegrityError("concurrent checkpoint change")
            db.executemany(
                "INSERT INTO events VALUES (?,?,?)",
                [(r["sequence"], r["content_hash"], json.dumps(r)) for r in rows],
            )

    def events(self, limit: int | None = None) -> list[dict]:
        with closing(self.connect()) as db:
            if limit is None:
                rows = db.execute("SELECT body FROM events ORDER BY sequence").fetchall()
            else:
                rows = db.execute(
                    "SELECT body FROM events ORDER BY sequence DESC LIMIT ?", (limit,)
                ).fetchall()[::-1]
        return [json.loads(row[0]) for row in rows]

    def setting(self, key: str, default=None):
        with closing(self.connect()) as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_status(self, status: dict) -> None:
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES ('status', ?)", (json.dumps(status),)
            )

    def analysis_sequence(self) -> int:
        return self.setting("analysis_sequence", -1)

    def save_analysis(self, sequence: int, findings: list[dict], coverage: dict) -> None:
        with closing(self.connect()) as db, db:
            db.executemany(
                "INSERT OR IGNORE INTO findings VALUES (?, ?)",
                [(f["id"], json.dumps(f)) for f in findings],
            )
            db.executemany(
                "INSERT OR REPLACE INTO settings VALUES (?, ?)",
                [("analysis_sequence", json.dumps(sequence)), ("coverage", json.dumps(coverage))],
            )

    def record_incident(self, finding: dict) -> None:
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT OR IGNORE INTO findings VALUES (?, ?)", (finding["id"], json.dumps(finding))
            )

    def findings(self) -> list[dict]:
        with closing(self.connect()) as db:
            rows = db.execute("SELECT body FROM findings ORDER BY rowid DESC LIMIT 1000").fetchall()
        return [json.loads(row[0]) for row in rows]


class Monitor:
    def __init__(self, source: Source, store: Store, keys: dict[str, str], batch_size: int = 100):
        if not keys or not 1 <= batch_size <= 200:
            raise ValueError("trusted keys and batch size 1..200 required")
        self.source, self.store, self.keys, self.batch_size = source, store, keys, batch_size

    async def ingest(self) -> None:
        checkpoint = self.store.checkpoint()
        seq, digest = checkpoint
        head = await self.source.page(limit=1)
        if head.get("chain_id") != self.store.chain_id:
            raise IntegrityError("unexpected chain id")
        head_seq = head.get("head_sequence")
        if type(head_seq) is not int or head_seq < max(seq, 0):
            raise IntegrityError("source chain truncated or uninitialized")
        if seq >= 0:
            anchor = await self.source.get(seq)
            verify_batch(
                [anchor], seq - 1, anchor.get("previous_hash"), self.store.chain_id, self.keys
            )
            if anchor["content_hash"] != digest:
                raise IntegrityError("source rewrote a committed checkpoint")
        end = min(head_seq, seq + self.batch_size)
        if end == seq:
            return
        page = await self.source.page(cursor=end, limit=end - seq)
        entries = page.get("entries", [])
        if page.get("chain_id") != self.store.chain_id or len(entries) != end - seq:
            raise IntegrityError("incomplete or mismatched page")
        clean = verify_batch(entries[::-1], seq, digest, self.store.chain_id, self.keys)
        self.store.append(clean, checkpoint)

    async def analyze_pending(self) -> None:
        from assurance.analysis import analyze

        sequence, _ = self.store.checkpoint()
        if sequence != self.store.analysis_sequence():
            events = self.store.events(limit=2000)
            findings, coverage = await analyze(events, self.store.chain_id)
            self.store.save_analysis(sequence, findings, coverage)

    async def cycle(self) -> None:
        from assurance.analysis import finding

        try:
            # Apply backpressure: failed analysis must not fall out of the bounded window.
            await self.analyze_pending()
            await self.ingest()
            await self.analyze_pending()
            self.store.set_status({"state": "ok", "checked_at": datetime.now(UTC).isoformat()})
        except Exception as exc:
            # Raw upstream exceptions may contain payloads or credentials: never publish them.
            state = "integrity_error" if isinstance(exc, IntegrityError) else "worker_error"
            if state == "integrity_error":
                seq, content_hash = self.store.checkpoint()
                incident = finding(
                    "ledger_integrity_failure",
                    "unattributed",
                    "ledger-verifier-v1",
                    [
                        {
                            "chain_id": self.store.chain_id,
                            "sequence": seq,
                            "content_hash": content_hash,
                        }
                    ],
                )
                incident["severity"] = "urgent"
                incident["evidence_role"] = "last trusted checkpoint; rejected data is not evidence"
                self.store.record_incident(incident)
            self.store.set_status(
                {
                    "state": state,
                    "error_type": type(exc).__name__,
                    "checked_at": datetime.now(UTC).isoformat(),
                }
            )
            raise
