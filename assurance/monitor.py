# SPDX-License-Identifier: Apache-2.0
"""Verified ingestion and durable, separate ingestion/analysis checkpoints."""

from __future__ import annotations

import hashlib
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
            if not isinstance(entry.payload, dict):
                raise IntegrityError("ledger payload must be an object")
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
                CREATE TABLE IF NOT EXISTS run_findings (
                    run_id TEXT NOT NULL, finding_id TEXT NOT NULL,
                    PRIMARY KEY (run_id, finding_id));
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, stream TEXT NOT NULL, config_digest TEXT NOT NULL,
                    sequence_end INTEGER NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS runs_stream ON runs(stream, config_digest, sequence_end);
                CREATE TABLE IF NOT EXISTS analysis_cache (
                    key TEXT PRIMARY KEY, result TEXT NOT NULL);
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

    def set_setting(self, key: str, value) -> None:
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value)))

    def progress(self, stream: str, config_digest: str) -> int:
        return self.setting(f"progress:{stream}:{config_digest}", -1)

    def stream_run(self, stream: str, config_digest: str) -> dict | None:
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT body FROM runs WHERE stream=? AND config_digest=? "
                "ORDER BY sequence_end DESC LIMIT 1",
                (stream, config_digest),
            ).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def evidence_digest(events: list[dict]) -> str:
        return hashlib.sha256(
            json.dumps(events, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def cached_analysis(self, key: str) -> dict | None:
        """Private successful requirement results, never exposed by results APIs."""
        with closing(self.connect()) as db:
            row = db.execute("SELECT result FROM analysis_cache WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def cache_analysis(self, key: str, result: dict) -> None:
        encoded = json.dumps(result)
        if len(encoded.encode()) > 1024 * 1024:
            raise ValueError("analysis cache result exceeds 1 MiB")
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR IGNORE INTO analysis_cache VALUES (?, ?)", (key, encoded))

    @staticmethod
    def _comparable_results(prior: dict, current: dict) -> bool:
        # A run-level replacement is not a finding resolution. Abstentions,
        # incomplete scopes and failures never supersede a completed assessment.
        if prior["status"] != "complete" or current["status"] != "complete":
            return False
        if prior["stream"] != current["stream"]:
            return False
        if Store.evidence_digest(prior["input_snapshot"]) != Store.evidence_digest(
            current["input_snapshot"]
        ):
            return False
        if current["stream"] == "ethos-divergence":
            old_requirements = prior.get("coverage", {}).get("requirements", [])
            new_requirements = current.get("coverage", {}).get("requirements", [])
            if not old_requirements or not new_requirements:
                return False
            for requirements in (old_requirements, new_requirements):
                if any(
                    r.get("status") != "complete" or r.get("excluded_traces", 0)
                    for r in requirements
                ):
                    return False
            # Same historical contract and eligible scope, even if a detector or
            # model configuration was deliberately changed. Different contracts
            # are distinct assessments, never a clearance of an older finding.
            if sorted(
                old_requirements, key=lambda r: (r.get("agent_id", ""), r.get("requirement_id", ""))
            ) != sorted(
                new_requirements, key=lambda r: (r.get("agent_id", ""), r.get("requirement_id", ""))
            ):
                return False
        return True

    def save_run(self, run: dict, findings: list[dict] | None = None) -> None:
        """Publish one stream outcome, findings, and its cursor atomically."""
        with closing(self.connect()) as db, db:
            if (
                run.get("status")
                in {"complete", "failed", "skipped", "insufficient_evidence", "not_configured"}
                and run.get("completed_at")
                and "model_requests" in run.get("coverage", {})
            ):
                # Keep actual request attempts across failed/restarted work. A
                # timeout's unknown usage cannot become a misleading zero total.
                row = db.execute("SELECT body FROM runs WHERE id=?", (run["id"],)).fetchone()
                prior_usage = json.loads(row[0]).get("inference_attempts", []) if row else []
                attempts = [u for u in prior_usage if u["started_at"] != run["started_at"]]
                attempts.append(
                    {
                        "started_at": run["started_at"],
                        "completed_at": run["completed_at"],
                        "model_requests": run["coverage"]["model_requests"],
                        "cached_requirement_checks": run["coverage"].get(
                            "cached_requirement_checks", 0
                        ),
                        "status": run["status"],
                    }
                )
                run["inference_attempts"] = attempts
                known = [
                    u["model_requests"] for u in attempts if type(u.get("model_requests")) is int
                ]
                run["known_model_requests"] = sum(known)
                run["model_requests_total"] = sum(known) if len(known) == len(attempts) else None
            if findings is not None and run.get("reanalysis"):
                previous = db.execute(
                    "SELECT body FROM runs WHERE stream=? AND sequence_end=? "
                    "AND config_digest<>? AND status='complete'",
                    (run["stream"], run["sequence_end"], run["config_digest"]),
                ).fetchall()
                supersedes = []
                for row in previous:
                    prior = json.loads(row[0])
                    if not self._comparable_results(prior, run):
                        continue
                    supersedes.append(prior["id"])
                    successors = prior.setdefault("superseded_by_run_ids", [])
                    if run["id"] not in successors:
                        successors.append(run["id"])
                    db.execute(
                        "UPDATE runs SET body=? WHERE id=?", (json.dumps(prior), prior["id"])
                    )
                run["supersedes_run_ids"] = sorted(supersedes)
                run["supersession_scope"] = "analysis_result_only; findings remain advisory history"
            db.execute(
                "INSERT OR REPLACE INTO runs VALUES (?, ?, ?, ?, ?, ?)",
                (
                    run["id"],
                    run["stream"],
                    run["config_digest"],
                    run["sequence_end"],
                    run["status"],
                    json.dumps(run),
                ),
            )
            if findings is not None:
                for item in findings:
                    body = {
                        **item,
                        "stream": run["stream"],
                        "run_id": run["id"],
                        "created_at": run["completed_at"],
                    }
                    db.execute(
                        "INSERT OR IGNORE INTO run_findings VALUES (?, ?)", (run["id"], body["id"])
                    )
                    # Stable detector IDs deduplicate observations across overlapping windows.
                    db.execute(
                        "INSERT OR IGNORE INTO findings VALUES (?, ?)",
                        (body["id"], json.dumps(body)),
                    )
                db.execute(
                    "INSERT OR REPLACE INTO settings VALUES (?, ?)",
                    (
                        f"progress:{run['stream']}:{run['config_digest']}",
                        json.dumps(run["sequence_end"]),
                    ),
                )
                if run["stream"] == "tool-issues":
                    db.executemany(
                        "INSERT OR REPLACE INTO settings VALUES (?, ?)",
                        [
                            ("analysis_sequence", json.dumps(run["sequence_end"])),
                            ("coverage", json.dumps(run["coverage"])),
                        ],
                    )

    def make_run(
        self,
        stream: str,
        config_digest: str,
        *,
        artifacts: dict,
        window_size: int = 2000,
        batch_size: int = 200,
    ) -> dict | None:
        previous = self.progress(stream, config_digest)
        head, _ = self.checkpoint()
        end = min(head, previous + batch_size)
        if end <= previous:
            return None
        start = max(0, end - window_size + 1)
        with closing(self.connect()) as db:
            rows = db.execute(
                "SELECT body FROM events WHERE sequence BETWEEN ? AND ? ORDER BY sequence",
                (start, end),
            ).fetchall()
            history_rows = db.execute(
                "SELECT body FROM runs WHERE stream=? AND config_digest<>? "
                "AND sequence_end>=? ORDER BY rowid DESC LIMIT 100",
                (stream, config_digest, start),
            ).fetchall()
        events = [json.loads(row[0]) for row in rows]
        previous_runs = [json.loads(row[0]) for row in history_rows]
        previous_runs = [r for r in previous_runs if r.get("evidence_start", 0) <= end]
        if len(events) != end - start + 1:
            raise IntegrityError("analysis snapshot has a missing event")
        identity = json.dumps([self.chain_id, stream, config_digest, end])
        run = {
            "id": hashlib.sha256(identity.encode()).hexdigest(),
            "stream": stream,
            "config_digest": config_digest,
            "sequence_start": previous + 1,
            "sequence_end": end,
            "evidence_start": start,
            "checkpoint": {"sequence": end, "content_hash": events[-1]["content_hash"]},
            "status": "pending",
            "attempts": 0,
            "coverage": {},
            "created_at": datetime.now(UTC).isoformat(),
            "started_at": None,
            "completed_at": None,
            "next_attempt": 0,
            "input_snapshot": events,
            "ethos_artifacts": artifacts,
            "evidence_digest": self.evidence_digest(events),
            "reanalysis": {
                "reason": "configuration_changed",
                "previous_run_ids": [r["id"] for r in previous_runs],
                "previous_config_digests": sorted({r["config_digest"] for r in previous_runs}),
                "ethos_policy": "historically_applicable_document_versions",
            }
            if previous_runs
            else None,
            "supersedes_run_ids": [],
            "superseded_by_run_ids": [],
        }
        self.save_run(run)
        return run

    def run(self, run_id: str) -> dict | None:
        with closing(self.connect()) as db:
            row = db.execute("SELECT body FROM runs WHERE id=?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def runs(self, limit=100, offset=0, *, stream="", status="", agent="", since="", until=""):
        params = (stream, stream, status, status, agent, agent, since, since, until, until)
        sql = """SELECT body FROM runs WHERE (?='' OR stream=?) AND (?='' OR status=?)
            AND (?='' OR EXISTS (SELECT 1 FROM json_each(json_extract(body, '$.agents'))
                                WHERE value=?))
            AND (?='' OR json_extract(body, '$.created_at')>=?)
            AND (?='' OR json_extract(body, '$.created_at')<=?)
            ORDER BY rowid DESC LIMIT ? OFFSET ?"""
        with closing(self.connect()) as db:
            rows = db.execute(sql, (*params, limit, offset)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def count_runs(self, *, stream="", status="", agent="", since="", until=""):
        with closing(self.connect()) as db:
            return db.execute(
                """SELECT COUNT(*) FROM runs
                WHERE (?='' OR stream=?) AND (?='' OR status=?)
                AND (?='' OR EXISTS (SELECT 1 FROM json_each(json_extract(body, '$.agents'))
                                    WHERE value=?))
                AND (?='' OR json_extract(body, '$.created_at')>=?)
                AND (?='' OR json_extract(body, '$.created_at')<=?)""",
                (stream, stream, status, status, agent, agent, since, since, until, until),
            ).fetchone()[0]

    def query_findings(
        self,
        limit=100,
        offset=0,
        *,
        agent="",
        stream="",
        run_id="",
        kind="",
        disposition="",
        since="",
        until="",
    ):
        params = (
            agent,
            agent,
            stream,
            stream,
            run_id,
            run_id,
            kind,
            kind,
            disposition,
            disposition,
            since,
            since,
            until,
            until,
        )
        # All values are bound parameters; column and JSON paths are fixed.
        where = """ WHERE (?='' OR json_extract(body, '$.agent_id')=?)
            AND (?='' OR json_extract(body, '$.stream')=?)
            AND (?='' OR EXISTS (SELECT 1 FROM run_findings r
                WHERE r.finding_id=findings.id AND r.run_id=?))
            AND (?='' OR json_extract(body, '$.kind')=?)
            AND (?='' OR json_extract(body, '$.disposition')=?)
            AND (?='' OR json_extract(body, '$.created_at')>=?)
            AND (?='' OR json_extract(body, '$.created_at')<=?)"""
        with closing(self.connect()) as db:
            total = db.execute(
                "SELECT COUNT(*) FROM findings" + where,  # noqa: S608  # nosec B608
                params,
            ).fetchone()[0]
            rows = db.execute(
                "SELECT body FROM findings" + where + " ORDER BY rowid DESC LIMIT ? OFFSET ?",  # noqa: S608  # nosec B608
                (*params, limit, offset),
            ).fetchall()
        return [json.loads(row[0]) for row in rows], total

    def get_finding(self, finding_id: str) -> dict | None:
        with closing(self.connect()) as db:
            row = db.execute("SELECT body FROM findings WHERE id=?", (finding_id,)).fetchone()
        if not row:
            return None
        result = json.loads(row[0])
        with closing(self.connect()) as db:
            result["history"] = [
                r[0]
                for r in db.execute(
                    "SELECT run_id FROM run_findings WHERE finding_id=? ORDER BY rowid",
                    (finding_id,),
                )
            ]
        return result

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
