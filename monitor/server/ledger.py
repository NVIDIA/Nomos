# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Read-only browser support for council ledger JSONL files.

The monitor intentionally does not import Nomos internals. It parses
the durable JSONL ledger format directly, recomputes canonical content hashes,
checks hash-chain links, and returns redacted summaries for the dashboard.
"""

from __future__ import annotations

import glob
import hashlib
import json
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_LEDGER_GLOB = "/app/ledger/*.jsonl"
SECRET_FIELD_PARTS = (
    "auth",
    "token",
    "secret",
    "password",
    "private_key",
    "api_key",
    "bearer",
    "credential",
)
MAX_STRING = 4096
DEFAULT_PAGE_LIMIT = 100
MAX_PAGE_LIMIT = 500
FILTER_SCAN_LIMIT = 5000


@dataclass(frozen=True)
class LedgerEntryView:
    chain_id: str
    source: str
    sequence: int
    entry_type: str
    timestamp: str
    previous_hash: str
    content_hash: str
    expected_hash: str
    hash_valid: bool
    link_valid: bool
    signature_count: int
    signer_ids: tuple[str, ...]
    payload: dict[str, Any]
    metadata: dict[str, Any]

    @property
    def intent_id(self) -> str:
        return _str(self.payload.get("intent_id") or self.payload.get("id"))

    @property
    def agent_id(self) -> str:
        return _str(self.payload.get("agent_id"))

    @property
    def decision(self) -> str:
        return _str(self.payload.get("decision") or self.payload.get("outcome"))

    @property
    def command(self) -> str:
        return _str(self.payload.get("command"))

    def summary(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "source": self.source,
            "sequence": self.sequence,
            "entry_type": self.entry_type,
            "timestamp": self.timestamp,
            "intent_id": self.intent_id,
            "agent_id": self.agent_id,
            "decision": self.decision,
            "command": self.command,
            "content_hash": self.content_hash,
            "previous_hash": self.previous_hash,
            "hash_valid": self.hash_valid,
            "link_valid": self.link_valid,
            "signature_count": self.signature_count,
            "signer_ids": list(self.signer_ids),
        }

    def detail(self) -> dict[str, Any]:
        return {
            **self.summary(),
            "expected_hash": self.expected_hash,
            "metadata": self.metadata,
            "payload": self.payload,
        }


@dataclass(frozen=True)
class LedgerChainView:
    chain_id: str
    source: str
    entries: tuple[LedgerEntryView, ...]
    is_valid: bool
    head_hash: str
    head_sequence: int
    broken_at: int | None

    def summary(self) -> dict[str, Any]:
        if len(self.entries) == 1 and self.entries[0].entry_type == "__summary__":
            entry = self.entries[0]
            entry_count = _int(entry.payload.get("entry_count"), 0)
            signed_count = _int(entry.metadata.get("signed_count"), 0)
            return {
                "chain_id": self.chain_id,
                "source": self.source,
                "entry_count": entry_count,
                "head_sequence": self.head_sequence,
                "head_hash": self.head_hash,
                "is_valid": self.is_valid,
                "broken_at": self.broken_at,
                "signed_count": signed_count,
                "type_counts": {},
                "fully_validated": False,
            }
        type_counts: dict[str, int] = {}
        signed_count = 0
        for entry in self.entries:
            type_counts[entry.entry_type] = type_counts.get(entry.entry_type, 0) + 1
            if entry.signature_count:
                signed_count += 1
        return {
            "chain_id": self.chain_id,
            "source": self.source,
            "entry_count": len(self.entries),
            "head_sequence": self.head_sequence,
            "head_hash": self.head_hash,
            "is_valid": self.is_valid,
            "broken_at": self.broken_at,
            "signed_count": signed_count,
            "type_counts": type_counts,
            "fully_validated": True,
        }


def load_ledger_snapshot(patterns: list[str] | None = None) -> dict[str, Any]:
    """Load all configured ledger files and return dashboard-ready JSON."""
    chains = [_load_chain(path) for path in _ledger_paths(patterns)]
    entries = [entry for chain in chains for entry in chain.entries]
    entries.sort(key=lambda entry: (entry.timestamp, entry.chain_id, entry.sequence), reverse=True)

    valid_chains = sum(1 for chain in chains if chain.is_valid)
    signed_entries = sum(1 for entry in entries if entry.signature_count)
    return {
        "chains": [chain.summary() for chain in chains],
        "entries": [entry.summary() for entry in entries],
        "next_cursor": "",
        "has_more": False,
        "page_limit": len(entries),
        "validation_mode": "full",
        "summary": {
            "chain_count": len(chains),
            "entry_count": len(entries),
            "valid_chain_count": valid_chains,
            "invalid_chain_count": len(chains) - valid_chains,
            "signed_entry_count": signed_entries,
            "unsigned_entry_count": max(0, len(entries) - signed_entries),
        },
    }


def load_ledger_page(
    *,
    patterns: list[str] | None = None,
    limit: int = DEFAULT_PAGE_LIMIT,
    cursor: str = "",
    chain_id: str = "",
    entry_type: str = "",
    query: str = "",
    integrity: str = "",
    validate: bool = False,
) -> dict[str, Any]:
    """Load one page of ledger entries without scanning entire files by default."""
    limit = max(1, min(limit, MAX_PAGE_LIMIT))
    paths = _ledger_paths(patterns)
    before = _decode_cursor(cursor)
    chains = [_load_chain(path) if validate else _summarize_chain(path) for path in paths]

    candidates: list[LedgerEntryView] = []
    scan_limit = FILTER_SCAN_LIMIT if any((entry_type, query, integrity)) else limit
    for chain in chains:
        if chain_id and chain.chain_id != chain_id:
            continue
        before_seq = before.get(chain.chain_id, chain.head_sequence + 1)
        candidates.extend(
            _read_entries_before(
                Path(chain.source),
                chain_id=chain.chain_id,
                before_sequence=before_seq,
                limit=limit,
                scan_limit=scan_limit,
                entry_type=entry_type,
                query=query,
                integrity=integrity,
            )
        )

    candidates.sort(
        key=lambda entry: (entry.timestamp, entry.chain_id, entry.sequence), reverse=True
    )
    page = candidates[:limit]
    next_before = dict(before)
    for entry in page:
        current = next_before.get(entry.chain_id)
        next_before[entry.chain_id] = (
            entry.sequence if current is None else min(current, entry.sequence)
        )
    has_more = any(
        chain.head_sequence >= 0 and next_before.get(chain.chain_id, chain.head_sequence + 1) > 0
        for chain in chains
        if not chain_id or chain.chain_id == chain_id
    )

    valid_chains = sum(1 for chain in chains if chain.is_valid)
    total_entries = sum(max(0, chain.head_sequence + 1) for chain in chains)
    signed_entries = sum(chain.summary().get("signed_count", 0) for chain in chains)
    return {
        "chains": [chain.summary() for chain in chains],
        "entries": [entry.summary() for entry in page],
        "next_cursor": _encode_cursor(next_before) if has_more and page else "",
        "has_more": has_more and bool(page),
        "page_limit": limit,
        "validation_mode": "full" if validate else "fast",
        "summary": {
            "chain_count": len(chains),
            "entry_count": total_entries,
            "valid_chain_count": valid_chains,
            "invalid_chain_count": len(chains) - valid_chains,
            "signed_entry_count": signed_entries,
            "unsigned_entry_count": max(0, total_entries - signed_entries),
        },
    }


def get_ledger_entry(
    *,
    chain_id: str,
    sequence: int,
    patterns: list[str] | None = None,
) -> dict[str, Any] | None:
    for path in _ledger_paths(patterns):
        chain = _summarize_chain(path)
        if chain.chain_id != chain_id:
            continue
        entry = _find_entry_by_sequence(path, chain_id=chain.chain_id, sequence=sequence)
        if entry:
            return entry.detail()
    return None


def _ledger_paths(patterns: list[str] | None) -> list[Path]:
    expanded: list[Path] = []
    for pattern in patterns or [DEFAULT_LEDGER_GLOB]:
        matches = glob.glob(pattern)
        if matches:
            expanded.extend(Path(path) for path in matches)
        else:
            path = Path(pattern)
            if path.exists():
                expanded.append(path)
    return sorted({path.resolve() for path in expanded if path.is_file()})


def _load_chain(path: Path) -> LedgerChainView:
    entries: list[LedgerEntryView] = []
    previous_hash = ""
    broken_at: int | None = None
    chain_id = path.stem

    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            continue
        if parsed.get("sequence") == 0:
            chain_id = _str((parsed.get("payload") or {}).get("chain_id")) or chain_id

        expected_hash = _hash_entry(parsed)
        content_hash = _str(parsed.get("content_hash"))
        previous = _str(parsed.get("previous_hash"))
        hash_valid = content_hash == expected_hash
        link_valid = previous == previous_hash if entries else previous == ""
        if broken_at is None and (not hash_valid or not link_valid):
            broken_at = _int(parsed.get("sequence"), len(entries))

        signatures = parsed.get("signatures")
        if not isinstance(signatures, list):
            signatures = []
        signer_ids = tuple(
            _str(sig.get("signer_id")) for sig in signatures if isinstance(sig, dict)
        )
        entry = LedgerEntryView(
            chain_id=chain_id,
            source=str(path),
            sequence=_int(parsed.get("sequence"), len(entries)),
            entry_type=_str(parsed.get("entry_type")),
            timestamp=_str(parsed.get("timestamp")),
            previous_hash=previous,
            content_hash=content_hash,
            expected_hash=expected_hash,
            hash_valid=hash_valid,
            link_valid=link_valid,
            signature_count=len(signatures),
            signer_ids=signer_ids,
            payload=_redact(parsed.get("payload")),
            metadata=_redact(parsed.get("metadata")),
        )
        entries.append(entry)
        previous_hash = content_hash

    head = entries[-1] if entries else None
    return LedgerChainView(
        chain_id=chain_id,
        source=str(path),
        entries=tuple(entries),
        is_valid=broken_at is None,
        head_hash=head.content_hash if head else "",
        head_sequence=head.sequence if head else -1,
        broken_at=broken_at,
    )


def _summarize_chain(path: Path) -> LedgerChainView:
    first = _first_json_line(path)
    last = _last_json_line(path)
    chain_id = _str((first.get("payload") or {}).get("chain_id")) if first else path.stem
    if not chain_id:
        chain_id = path.stem
    if not last:
        return LedgerChainView(
            chain_id=chain_id,
            source=str(path),
            entries=(),
            is_valid=True,
            head_hash="",
            head_sequence=-1,
            broken_at=None,
        )
    head = _entry_from_raw(last, chain_id=chain_id, expected_previous=None)
    entry_count = max(0, head.sequence + 1)
    signed_count = entry_count if head.signature_count else 0
    summary_entry = LedgerEntryView(
        **{
            **head.__dict__,
            "source": str(path),
            "sequence": entry_count - 1,
            "entry_type": "__summary__",
            "payload": {"entry_count": entry_count},
            "metadata": {"signed_count": signed_count},
        }
    )
    return LedgerChainView(
        chain_id=chain_id,
        source=str(path),
        entries=(summary_entry,),
        is_valid=head.hash_valid,
        head_hash=head.content_hash,
        head_sequence=head.sequence,
        broken_at=None if head.hash_valid else head.sequence,
    )


def _read_entries_before(
    path: Path,
    *,
    chain_id: str,
    before_sequence: int,
    limit: int,
    scan_limit: int,
    entry_type: str,
    query: str,
    integrity: str,
) -> list[LedgerEntryView]:
    entries: list[LedgerEntryView] = []
    scanned = 0
    previous_by_sequence: dict[int, str] = {}
    for parsed in _iter_json_reverse(path):
        sequence = _int(parsed.get("sequence"), -1)
        if sequence < 0 or sequence >= before_sequence:
            continue
        scanned += 1
        expected_previous = previous_by_sequence.get(sequence)
        entry = _entry_from_raw(parsed, chain_id=chain_id, expected_previous=expected_previous)
        previous_by_sequence[sequence - 1] = entry.previous_hash
        if _entry_matches(entry, entry_type=entry_type, query=query, integrity=integrity):
            entries.append(entry)
            if len(entries) >= limit:
                break
        if scanned >= scan_limit:
            break
    return entries


def _find_entry_by_sequence(path: Path, *, chain_id: str, sequence: int) -> LedgerEntryView | None:
    next_previous = None
    for parsed in _iter_json_reverse(path):
        parsed_sequence = _int(parsed.get("sequence"), -1)
        if parsed_sequence < sequence:
            break
        if parsed_sequence == sequence + 1:
            next_previous = _str(parsed.get("previous_hash"))
        if parsed_sequence == sequence:
            return _entry_from_raw(parsed, chain_id=chain_id, expected_previous=next_previous)
    return None


def _entry_from_raw(
    parsed: dict[str, Any],
    *,
    chain_id: str,
    expected_previous: str | None,
) -> LedgerEntryView:
    expected_hash = _hash_entry(parsed)
    content_hash = _str(parsed.get("content_hash"))
    previous = _str(parsed.get("previous_hash"))
    sequence = _int(parsed.get("sequence"), -1)
    hash_valid = content_hash == expected_hash
    if sequence == 0:
        link_valid = previous == ""
    elif expected_previous is None:
        link_valid = True
    else:
        link_valid = content_hash == expected_previous
    signatures = parsed.get("signatures")
    if not isinstance(signatures, list):
        signatures = []
    signer_ids = tuple(_str(sig.get("signer_id")) for sig in signatures if isinstance(sig, dict))
    return LedgerEntryView(
        chain_id=chain_id,
        source="",
        sequence=sequence,
        entry_type=_str(parsed.get("entry_type")),
        timestamp=_str(parsed.get("timestamp")),
        previous_hash=previous,
        content_hash=content_hash,
        expected_hash=expected_hash,
        hash_valid=hash_valid,
        link_valid=link_valid,
        signature_count=len(signatures),
        signer_ids=signer_ids,
        payload=_redact(parsed.get("payload")),
        metadata=_redact(parsed.get("metadata")),
    )


def _entry_matches(
    entry: LedgerEntryView,
    *,
    entry_type: str,
    query: str,
    integrity: str,
) -> bool:
    if entry_type and entry.entry_type != entry_type:
        return False
    if integrity == "valid" and (not entry.hash_valid or not entry.link_valid):
        return False
    if integrity == "broken" and entry.hash_valid and entry.link_valid:
        return False
    if integrity == "unsigned" and entry.signature_count > 0:
        return False
    if query and query.lower() not in _entry_search_text(entry):
        return False
    return True


def _entry_search_text(entry: LedgerEntryView) -> str:
    return " ".join(
        [
            entry.chain_id,
            entry.entry_type,
            entry.intent_id,
            entry.agent_id,
            entry.decision,
            entry.command,
            entry.content_hash,
            entry.previous_hash,
            " ".join(entry.signer_ids),
        ]
    ).lower()


def _first_json_line(path: Path) -> dict[str, Any] | None:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                parsed = json.loads(line)
                return parsed if isinstance(parsed, dict) else None
    return None


def _last_json_line(path: Path) -> dict[str, Any] | None:
    for parsed in _iter_json_reverse(path):
        return parsed
    return None


def _iter_json_reverse(path: Path):
    for line in _iter_lines_reverse(path):
        if not line.strip():
            continue
        parsed = json.loads(line)
        if isinstance(parsed, dict):
            yield parsed


def _iter_lines_reverse(path: Path, block_size: int = 1024 * 64):
    with path.open("rb") as handle:
        handle.seek(0, 2)
        position = handle.tell()
        buffer = b""
        while position > 0:
            read_size = min(block_size, position)
            position -= read_size
            handle.seek(position)
            chunk = handle.read(read_size)
            buffer = chunk + buffer
            lines = buffer.split(b"\n")
            buffer = lines[0]
            for line in reversed(lines[1:]):
                if line:
                    yield line.decode("utf-8", errors="replace")
        if buffer:
            yield buffer.decode("utf-8", errors="replace")


def _encode_cursor(before: dict[str, int]) -> str:
    if not before:
        return ""
    raw = json.dumps({"before": before}, sort_keys=True, separators=(",", ":")).encode()
    return urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> dict[str, int]:
    if not cursor:
        return {}
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = json.loads(urlsafe_b64decode(padded.encode()).decode())
    except Exception:
        return {}
    before = decoded.get("before") if isinstance(decoded, dict) else None
    if not isinstance(before, dict):
        return {}
    return {
        str(key): value
        for key, value in before.items()
        if isinstance(value, int) and not isinstance(value, bool)
    }


def _hash_entry(entry: dict[str, Any]) -> str:
    content = {
        "sequence": entry.get("sequence"),
        "entry_type": entry.get("entry_type"),
        "payload": entry.get("payload"),
        "timestamp": entry.get("timestamp"),
        "previous_hash": entry.get("previous_hash"),
    }
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, child in value.items():
            key_str = str(key)
            lowered = key_str.lower()
            if any(part in lowered for part in SECRET_FIELD_PARTS):
                out[key_str] = "[redacted]"
            else:
                out[key_str] = _redact(child)
        return out
    if isinstance(value, list):
        return [_redact(child) for child in value]
    if isinstance(value, str):
        return value if len(value) <= MAX_STRING else value[:MAX_STRING] + "...[truncated]"
    return value


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _int(value: Any, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default
