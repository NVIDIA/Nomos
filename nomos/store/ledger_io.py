"""
Ledger import/export between JSONL files and PostgreSQL.

These are the Proposal 07 cutover tools:

    python -m nomos.store.ledger_io import council-ledger.jsonl \
        --chain-id ledger-council-prod [--dsn postgresql://...]
    python -m nomos.store.ledger_io export ledger-council-prod \
        --out council-ledger.jsonl [--dsn postgresql://...]

Import validates the chain hash-by-hash (contiguous sequences, intact
previous-hash links, recomputed content hashes) and refuses gaps, forks, or a
non-empty target chain. Export emits the exact LedgerEntry.to_dict() JSONL
shape consumed by existing audit tooling and the file-mode monitor.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from pathlib import Path

from nomos.level_0.crypto.hashing import ContentHasher
from nomos.level_1.ledger.entry import LedgerEntry

logger = logging.getLogger(__name__)

_INSERT_COLUMNS = (
    "chain_id, sequence, entry_type, payload_json, metadata_json, previous_hash, "
    "entry_hash, signatures_json, signer_key_id, writer_instance_id, entry_timestamp_iso"
)


class LedgerImportError(Exception):
    """The JSONL chain is invalid or the target chain is not empty."""


def load_and_validate_jsonl(path: Path) -> list[LedgerEntry]:
    """Parse a JSONL ledger file and verify it forms one valid chain."""
    entries: list[LedgerEntry] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entries.append(LedgerEntry.from_dict(json.loads(line)))
        except Exception as exc:
            raise LedgerImportError(f"{path}:{line_number}: unparseable entry: {exc}") from exc

    if not entries:
        raise LedgerImportError(f"{path}: no ledger entries found")

    hasher = ContentHasher()
    previous_hash = None
    for index, entry in enumerate(entries):
        if entry.sequence != index:
            raise LedgerImportError(
                f"sequence gap or reorder at line {index + 1}: expected {index}, "
                f"got {entry.sequence}"
            )
        if index == 0 and entry.previous_hash is not None:
            raise LedgerImportError("genesis entry must have no previous hash")
        if index > 0 and entry.previous_hash != previous_hash:
            raise LedgerImportError(
                f"broken hash link at sequence {entry.sequence}: previous_hash does not "
                "match prior entry hash (fork or tampering)"
            )
        recomputed = hasher.hash_json(entry.content_for_hashing())
        if entry.content_hash != recomputed:
            raise LedgerImportError(
                f"content hash mismatch at sequence {entry.sequence}: recomputed "
                f"{recomputed} != stored {entry.content_hash}"
            )
        previous_hash = entry.content_hash
    return entries


def import_jsonl(dsn: str, path: Path, chain_id: str, *, council_id: str = "") -> int:
    """Import a validated JSONL chain into an empty PostgreSQL chain."""
    import psycopg
    from psycopg.types.json import Json

    entries = load_and_validate_jsonl(path)
    council = council_id or chain_id.removeprefix("ledger-")
    writer = f"ledger-io-{uuid.uuid4().hex[:8]}"

    with psycopg.connect(dsn) as conn, conn.transaction():
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (chain_id,),
        )
        row = conn.execute(
            "SELECT current_head_sequence FROM ledger_chains WHERE chain_id = %s FOR UPDATE",
            (chain_id,),
        ).fetchone()
        if row is not None and row[0] is not None:
            raise LedgerImportError(
                f"chain {chain_id} already has entries (head sequence {row[0]}); "
                "refusing to import over an existing chain"
            )
        conn.execute(
            "INSERT INTO ledger_chains (chain_id, council_id, created_by) "
            "VALUES (%s, %s, %s) ON CONFLICT (chain_id) DO NOTHING",
            (chain_id, council, writer),
        )
        for entry in entries:
            data = entry.to_dict()
            conn.execute(
                f"INSERT INTO ledger_entries ({_INSERT_COLUMNS}) "  # noqa: S608 # nosec B608 - static cols
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    chain_id,
                    entry.sequence,
                    data["entry_type"],
                    Json(data["payload"]),
                    Json(data["metadata"]),
                    data["previous_hash"],
                    data["content_hash"],
                    Json(data["signatures"]),
                    entry.signatures[0].signer_id if entry.signatures else None,
                    writer,
                    data["timestamp"],
                ),
            )
        head = entries[-1]
        conn.execute(
            "UPDATE ledger_chains SET current_head_sequence = %s, current_head_hash = %s "
            "WHERE chain_id = %s",
            (head.sequence, str(head.content_hash), chain_id),
        )
    return len(entries)


def export_jsonl(dsn: str, chain_id: str, out_path: Path) -> int:
    """Export a PostgreSQL chain as LedgerEntry.to_dict() JSONL."""
    import psycopg

    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT sequence, entry_type, payload_json, metadata_json, previous_hash, "
            "entry_hash, signatures_json, entry_timestamp_iso FROM ledger_entries "
            "WHERE chain_id = %s ORDER BY sequence",
            (chain_id,),
        ).fetchall()

    lines = []
    for sequence, entry_type, payload, metadata, previous_hash, entry_hash, sigs, ts in rows:
        entry = LedgerEntry.from_dict(
            {
                "sequence": sequence,
                "entry_type": entry_type,
                "payload": payload,
                "metadata": metadata,
                "previous_hash": previous_hash,
                "content_hash": entry_hash,
                "signatures": sigs,
                "timestamp": ts,
            }
        )
        lines.append(json.dumps(entry.to_dict()))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return len(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Import/export Nomos ledger chains")
    parser.add_argument(
        "--dsn",
        default=os.environ.get("COUNCIL_DATABASE_URL", ""),
        help="PostgreSQL DSN (default: COUNCIL_DATABASE_URL)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    import_parser = sub.add_parser("import", help="Import a JSONL ledger into PostgreSQL")
    import_parser.add_argument("jsonl", type=Path, help="Path to the JSONL ledger file")
    import_parser.add_argument("--chain-id", required=True, help="Target chain id")
    import_parser.add_argument("--council-id", default="", help="Owning council id")

    export_parser = sub.add_parser("export", help="Export a PostgreSQL chain to JSONL")
    export_parser.add_argument("chain_id", help="Chain id to export")
    export_parser.add_argument("--out", type=Path, required=True, help="Output JSONL path")

    args = parser.parse_args()
    if not args.dsn:
        parser.error("--dsn or COUNCIL_DATABASE_URL is required")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        if args.command == "import":
            count = import_jsonl(args.dsn, args.jsonl, args.chain_id, council_id=args.council_id)
            print(f"imported {count} entries into chain {args.chain_id}")
        else:
            count = export_jsonl(args.dsn, args.chain_id, args.out)
            print(f"exported {count} entries from chain {args.chain_id} to {args.out}")
    except LedgerImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
