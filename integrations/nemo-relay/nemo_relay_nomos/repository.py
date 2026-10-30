# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Per-activation, in-memory SQLite repository.

Evidence records, the call journal, writ claims, turns and conversations live
in one SQLite ``:memory:`` database owned by the worker process. Nothing is
written to disk, and nothing outlives the worker: a new activation starts empty.
Records are immutable BLOBs written in the same transaction as the journal
entry that produced them.

One worker can serve many conversations, and Relay tells it nothing about who
is calling. A conversation is therefore identified by the turn tokens its own
transcript carries (see ``open_turn``), and every piece of session state (the
journal, the denial count, the blocked flag, quotas) is kept per conversation.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass

from .records import CATALOG_KINDS
from .settings import Limits
from .wire import canonical, sha256_hex

MAX_REFUSAL_RECORDS = 200
_UNFINISHED = {"dispatching": "execution_unknown", "authorizing": "authorization_interrupted"}


class StoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class Turn:
    """One model request: the conversation it belongs to and what the plugin knew about it."""

    token: str
    conversation: str
    citable: frozenset[str]  # conversations whose evidence this turn may cite
    user_request: str
    user_request_truncated: bool = False
    tools_sha256: str | None = None  # the request's tool list, when tool_definitions is on (see tool_list)
    tools_kept: bool = False  # whether that list was admitted for this turn's conversation


class EvidenceStore:
    def __init__(self, owner: tuple[str, str, str], limits: Limits):
        self.owner, self.limits = owner, limits
        self.db: sqlite3.Connection | None = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE conversations (
                id TEXT PRIMARY KEY, created REAL NOT NULL,
                denials INTEGER NOT NULL DEFAULT 0, blocked INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE turns (
                token TEXT PRIMARY KEY, conversation TEXT NOT NULL, citable TEXT NOT NULL,
                user_request TEXT NOT NULL, user_request_truncated INTEGER NOT NULL,
                created REAL NOT NULL, expires REAL NOT NULL, tools_sha256 TEXT,
                tools_kept INTEGER NOT NULL DEFAULT 0, superseded INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE tool_lists (sha256 TEXT PRIMARY KEY, tools BLOB NOT NULL);
            CREATE TABLE records (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, conversation TEXT,
                kind TEXT NOT NULL, status TEXT NOT NULL,
                created REAL NOT NULL, expires REAL NOT NULL,
                record BLOB NOT NULL, digest TEXT NOT NULL,
                citation TEXT NOT NULL DEFAULT 'not_submitted');
            CREATE TABLE calls (
                conversation TEXT NOT NULL, id TEXT NOT NULL, request_hash TEXT NOT NULL, state TEXT NOT NULL,
                outcome BLOB, digest TEXT, created REAL NOT NULL, PRIMARY KEY (conversation, id));
            CREATE TABLE writ_claims (
                nonce TEXT PRIMARY KEY, writ_id TEXT NOT NULL, claimed REAL NOT NULL);
        """)

    def close(self) -> None:
        if self.db is not None:
            self.db.close()
            self.db = None

    # ── turns and conversations ──────────────────────────────────────────

    @staticmethod
    def new_turn_token() -> str:
        return "nt_" + uuid.uuid4().hex

    def open_turn(
        self,
        seen_tokens: list[str],
        user_request: str,
        tools: list | None = None,
        *,
        user_request_truncated: bool = False,
    ) -> Turn:
        """Issue the token for one model request, in the conversation its transcript shows.

        ``seen_tokens`` are the turn tokens found in the request's messages. None known: a new
        conversation. One conversation: continue it. Several: a new conversation that inherits the
        strictest state among them (blocked if any is, the highest denial count) and may cite their
        evidence; the conversations found are left unchanged. ``tools``, when given, is the request's tool list:
        the turn records its digest, and the list is kept once (see ``_keep_tool_list``).
        """
        now = time.time()
        with self.db:
            self._purge(now)
            found = sorted(
                {
                    row["conversation"]
                    for token in dict.fromkeys(seen_tokens)
                    for row in self.db.execute("SELECT conversation FROM turns WHERE token=?", (token,))
                }
            )
            if len(found) == 1:
                conversation, citable = found[0], [found[0]]
            else:
                conversation = "cv_" + uuid.uuid4().hex
                denials, blocked = 0, 0
                if found:
                    marks = ",".join("?" * len(found))
                    denials, blocked = self.db.execute(
                        f"SELECT max(denials), max(blocked) FROM conversations WHERE id IN ({marks})", found
                    ).fetchone()
                self.db.execute(
                    "INSERT INTO conversations VALUES (?, ?, ?, ?)", (conversation, now, denials, blocked)
                )
                citable = [conversation, *found]
            # This request follows every turn its messages show: a call from those turns' replies is over.
            seen = list(dict.fromkeys(seen_tokens))
            if seen:
                marks = ",".join("?" * len(seen))
                self.db.execute(f"UPDATE turns SET superseded=1 WHERE token IN ({marks})", seen)
            token = self.new_turn_token()
            tools_sha256, tools_kept = None, False
            if tools is not None:
                blob = canonical(tools)
                tools_sha256 = sha256_hex(blob)
                tools_kept = self._keep_tool_list(conversation, tools_sha256, blob)
            self.db.execute(
                "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    token,
                    conversation,
                    canonical(citable).decode(),
                    user_request,
                    int(user_request_truncated),
                    now,
                    now + self.limits.retention_seconds,
                    tools_sha256,
                    int(tools_kept),
                ),
            )
            # Bounded: the oldest tokens go first; a model holding one is taught to use its latest token.
            self.db.execute(
                "DELETE FROM turns WHERE rowid IN (SELECT rowid FROM turns ORDER BY rowid DESC LIMIT -1 OFFSET ?)",
                (self.limits.max_invocations,),
            )
            self._drop_unnamed_tool_lists()
        return Turn(
            token,
            conversation,
            frozenset(citable),
            user_request,
            user_request_truncated,
            tools_sha256,
            tools_kept,
        )

    def turn(self, token: str) -> Turn | None:
        """The live turn for a token, or None when it is unknown or expired."""
        row = self.db.execute(
            "SELECT * FROM turns WHERE token=? AND expires>?", (token, time.time())
        ).fetchone()
        if row is None:
            return None
        return Turn(
            row["token"],
            row["conversation"],
            frozenset(json.loads(row["citable"])),
            row["user_request"],
            bool(row["user_request_truncated"]),
            row["tools_sha256"],
            bool(row["tools_kept"]),
        )

    def is_superseded(self, turn: Turn) -> bool:
        """Whether a later model request's messages showed ``turn``'s token: its transcript has moved past it.

        A request that does not show it (another fork of the same transcript, or a concurrent request) leaves it
        current, so each request's calls may name their own turn."""
        row = self.db.execute("SELECT superseded FROM turns WHERE token=?", (turn.token,)).fetchone()
        return row is None or bool(row["superseded"])

    def tool_list(self, sha256: str) -> list | None:
        """A tool list a live turn names, by its digest; None when it was not kept."""
        row = self.db.execute("SELECT tools FROM tool_lists WHERE sha256=?", (sha256,)).fetchone()
        return json.loads(row["tools"]) if row else None

    def _keep_tool_list(self, conversation: str, sha256: str, blob: bytes) -> bool:
        """Admit a tool list for one conversation's turn: stored once, charged to each conversation that keeps it.

        It must be no larger than one citation may be and fit the quota: store-wide when the list is new, and in
        the conversation's share unless one of its turns already keeps it. A list that does not fit is not kept
        for this turn: the turn still names its digest, and H3 reports it unavailable.
        """
        if len(blob) > self.limits.max_citation_bytes:
            return False
        if self.db.execute(
            "SELECT 1 FROM turns WHERE conversation=? AND tools_sha256=? AND tools_kept=1",
            (conversation, sha256),
        ).fetchone():
            return True
        stored = self.db.execute("SELECT 1 FROM tool_lists WHERE sha256=?", (sha256,)).fetchone() is not None
        try:
            self._check_quota(
                conversation, extra_calls=0, extra_bytes=(0 if stored else len(blob), len(blob))
            )
        except StoreError:
            return False
        if not stored:
            self.db.execute("INSERT INTO tool_lists VALUES (?, ?)", (sha256, blob))
        return True

    def _drop_unnamed_tool_lists(self) -> None:
        """A tool list no live turn keeps is never read again."""
        self.db.execute(
            "DELETE FROM tool_lists WHERE sha256 NOT IN (SELECT tools_sha256 FROM turns WHERE tools_kept=1)"
        )

    def denials(self, conversation: str) -> int:
        row = self.db.execute("SELECT denials FROM conversations WHERE id=?", (conversation,)).fetchone()
        return row["denials"] if row else 0

    def set_denials(self, conversation: str, count: int) -> None:
        with self.db:
            self.db.execute("UPDATE conversations SET denials=? WHERE id=?", (count, conversation))

    # ── call journal ─────────────────────────────────────────────────────

    @staticmethod
    def new_id() -> str:
        return "ev_" + uuid.uuid4().hex

    def begin(self, conversation: str, call_id: str, request_hash: str) -> dict | None:
        """Reserve capacity before any network request. None means a new call."""
        with self.db:
            row = self.db.execute(
                "SELECT * FROM calls WHERE conversation=? AND id=?", (conversation, call_id)
            ).fetchone()
            if row:
                if row["request_hash"] != request_hash:
                    raise StoreError("tool_call_id_conflict")
                if row["state"] != "complete":
                    raise StoreError(_UNFINISHED.get(row["state"], "journal_integrity_failure"))
                if sha256_hex(row["outcome"]) != row["digest"]:
                    raise StoreError("journal_integrity_failure")
                return json.loads(row["outcome"])
            blocked = self.db.execute(
                "SELECT blocked FROM conversations WHERE id=?", (conversation,)
            ).fetchone()
            if (blocked and blocked["blocked"]) or self.db.execute(
                "SELECT 1 FROM calls WHERE conversation=? AND state='dispatching' LIMIT 1", (conversation,)
            ).fetchone():
                raise StoreError("session_requires_reconciliation")
            self._purge(time.time())
            self._check_quota(conversation, extra_calls=1)
            self.db.execute(
                "INSERT INTO calls VALUES (?, ?, ?, 'authorizing', NULL, NULL, ?)",
                (conversation, call_id, request_hash, time.time()),
            )
        return None

    def dispatch(self, conversation: str, call_id: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE calls SET state='dispatching' WHERE conversation=? AND id=?", (conversation, call_id)
            )

    def finish(self, conversation: str, call_id: str, outcome: dict, record: dict | None = None) -> None:
        """Persist the immutable record and the replay outcome together, before exposure."""
        encoded = canonical(outcome)
        with self.db:
            if record is not None:
                self._insert(record, conversation)
            self.db.execute(
                "UPDATE calls SET state='complete', outcome=?, digest=? WHERE conversation=? AND id=?",
                (encoded, sha256_hex(encoded), conversation, call_id),
            )
            if outcome.get("execution_status") == "unknown":
                self.db.execute("UPDATE conversations SET blocked=1 WHERE id=?", (conversation,))

    # ── evidence records ─────────────────────────────────────────────────

    def put(self, record: dict, conversation: str | None = None) -> None:
        """Store a record outside the journal. ``conversation`` is None when the call named no live turn."""
        with self.db:
            if record["kind"] == "plugin_refusal":
                # Refusals cost the model nothing to produce; keep only each conversation's most recent ones so
                # a flood of them can never exhaust the record quota and lock governed tools.
                self.db.execute(
                    "DELETE FROM records WHERE kind='plugin_refusal' AND conversation IS ? AND seq NOT IN "
                    "(SELECT seq FROM records WHERE kind='plugin_refusal' AND conversation IS ? "
                    "ORDER BY seq DESC LIMIT ?)",
                    (conversation, conversation, MAX_REFUSAL_RECORDS - 1),
                )
            else:
                self._check_quota(conversation, extra_calls=0)
            self._insert(record, conversation)

    def get(self, evidence_id: str, citable: frozenset[str] | None = None) -> dict:
        """A stored record. With ``citable``, a record from any other conversation reads as not found."""
        row = self.db.execute("SELECT * FROM records WHERE id=?", (evidence_id,)).fetchone()
        if row is None or (citable is not None and row["conversation"] not in citable):
            # Another conversation's IDs, like another worker's, are indistinguishable from unknown IDs.
            raise StoreError("evidence_not_found")
        if row["expires"] <= time.time():
            with self.db:
                self._purge(time.time())
            raise StoreError("evidence_expired")
        if sha256_hex(row["record"]) != row["digest"]:
            raise StoreError("evidence_integrity_failure")
        record = json.loads(row["record"])
        record.update(
            council_citation_status=row["citation"], expires_at=row["expires"], parent_digest=row["digest"]
        )
        return record

    def catalog(self, visible: list[str], citable: frozenset[str], limit: int) -> list[dict]:
        """Citable records among ``visible`` (the IDs a request's own messages show), newest first."""
        ids = list(dict.fromkeys(visible))
        if not ids or not citable:
            return []
        kinds, id_marks, conv_marks = (
            ",".join("?" * n) for n in (len(CATALOG_KINDS), len(ids), len(citable))
        )
        rows = self.db.execute(
            f"SELECT id FROM records WHERE expires>? AND status!='invalid' AND kind IN ({kinds}) "
            f"AND id IN ({id_marks}) AND conversation IN ({conv_marks}) ORDER BY seq DESC LIMIT ?",
            (time.time(), *CATALOG_KINDS, *ids, *sorted(citable), limit),
        ).fetchall()
        out = []
        for row in rows:
            try:
                out.append(self.get(row["id"]))
            except StoreError:
                continue  # an expired or corrupt record is simply not offered
        return out

    def mark_citations(self, ids: list[str], status: str) -> None:
        with self.db:
            self.db.executemany(
                "UPDATE records SET citation=? WHERE id=? AND citation!='accepted'",
                [(status, i) for i in ids],
            )

    def claim_writ(self, writ_id: str, nonce: str) -> bool:
        with self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO writ_claims VALUES (?, ?, ?)", (nonce, writ_id, time.time())
            )
        return cursor.rowcount == 1

    # ── internals ────────────────────────────────────────────────────────

    def _insert(self, record: dict, conversation: str | None) -> None:
        blob = canonical(record)
        self.db.execute(
            "INSERT INTO records (id, conversation, kind, status, created, expires, record, digest) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record["evidence_id"],
                conversation,
                record["kind"],
                record["evidence_status"],
                record["created_at"],
                record["created_at"] + self.limits.retention_seconds,
                blob,
                sha256_hex(blob),
            ),
        )

    def _purge(self, now: float) -> None:
        """Retention for a worker that may live for weeks: nothing grows without bound."""
        retention = self.limits.retention_seconds
        # An expired record keeps an ID tombstone for one more retention period, so a caller can tell expiry
        # from an unknown ID; then the row goes.
        self.db.execute(
            "UPDATE records SET record=x'', digest='' WHERE expires<=? AND length(record)>0", (now,)
        )
        self.db.execute("DELETE FROM records WHERE expires<=?", (now - retention,))
        self.db.execute("DELETE FROM calls WHERE state='complete' AND created<=?", (now - retention,))
        self.db.execute("DELETE FROM turns WHERE expires<=?", (now,))
        self._drop_unnamed_tool_lists()
        # A conversation no live turn names can never be reached again, unless a call is still open in it.
        self.db.execute(
            "DELETE FROM conversations WHERE id NOT IN (SELECT conversation FROM turns) "
            "AND id NOT IN (SELECT conversation FROM calls WHERE state!='complete')"
        )

    def _check_quota(
        self, conversation: str | None, *, extra_calls: int, extra_bytes: tuple[int, int] = (0, 0)
    ) -> None:
        """Store-wide caps, plus a per-conversation share so one conversation cannot fill the store.

        Kept tool lists count too: all of them store-wide, and each list a conversation's turns keep in its share.
        ``extra_bytes`` is (store-wide, conversation) bytes about to be added.
        """
        share = self.limits.conversation_share_percent / 100
        scopes = (
            ("1", (), 1.0, extra_bytes[0]),
            ("conversation IS ?", (conversation,), share, extra_bytes[1]),
        )
        for where, params, fraction, extra in scopes:
            calls, in_flight, journal = self.db.execute(
                f"SELECT count(*), coalesce(sum(state!='complete'), 0), coalesce(sum(length(outcome)), 0) "
                f"FROM calls WHERE {where}",
                params,
            ).fetchone()
            count, size = self.db.execute(
                "SELECT coalesce(sum(kind != 'plugin_refusal'), 0), coalesce(sum(length(record)), 0) "
                f"FROM records WHERE length(record)>0 AND {where}",
                params,
            ).fetchone()
            lists = self.db.execute(
                "SELECT coalesce(sum(length(tools)), 0) FROM tool_lists"
                + (
                    ""
                    if where == "1"
                    else f" WHERE sha256 IN (SELECT tools_sha256 FROM turns WHERE {where} AND tools_kept=1)"
                ),
                params,
            ).fetchone()[0]
            size += lists + extra
            journal += calls * 1024  # bounded IDs, hashes and index overhead
            # Every call in flight may still store one full result; hold that space back for each of them.
            reserve = self.limits.reserve_bytes * max(1, in_flight + extra_calls)
            if (
                calls + extra_calls > self.limits.max_invocations * fraction
                or count >= self.limits.max_records * fraction
                or size + journal + reserve > self.limits.max_storage_bytes * fraction
            ):
                raise StoreError("storage_quota_exceeded")
