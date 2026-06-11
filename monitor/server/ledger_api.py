"""Council-API ledger source for the monitor.

In PVC-free deployments the monitor holds no database credentials and mounts
no ledger volume: it reads ledger entries through the council's
`council_ledger_page` / `council_ledger_get` tools (NAT debug endpoint) and
renders them through the same view/verification layer as file mode
(hash re-verification, link checks, redaction).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from monitor.server.ledger import (
    DEFAULT_PAGE_LIMIT,
    FILTER_SCAN_LIMIT,
    MAX_PAGE_LIMIT,
    LedgerChainView,
    LedgerEntryView,
    _decode_cursor,
    _encode_cursor,
    _entry_from_raw,
    _entry_matches,
)

logger = logging.getLogger(__name__)


async def _call_tool(
    client: httpx.AsyncClient,
    api_url: str,
    tool: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Invoke a council tool via the NAT debug endpoint and decode its result."""
    url = f"{api_url.rstrip('/')}/debug/tools/call/{tool}"
    response = await client.post(url, json=payload, timeout=10.0)
    response.raise_for_status()
    body = response.json()
    if isinstance(body, str):
        body = json.loads(body)
    result = body.get("result", body)
    if isinstance(result, str):
        result = json.loads(result)
    if not isinstance(result, dict):
        raise ValueError(f"unexpected {tool} response shape")
    return result


def _views_from_page(raw_entries: list[dict[str, Any]], chain_id: str) -> list[LedgerEntryView]:
    """Build entry views from a newest-first page, chaining link expectations.

    Entry N's content hash must equal entry N+1's previous_hash; within one
    contiguous newest-first page the successor of entry i is element i-1.
    """
    views: list[LedgerEntryView] = []
    for index, parsed in enumerate(raw_entries):
        expected_previous: str | None = None
        if index > 0:
            newer = raw_entries[index - 1]
            if newer.get("sequence") == (parsed.get("sequence") or 0) + 1:
                expected_previous = str(newer.get("previous_hash") or "")
        views.append(
            _entry_from_raw(parsed, chain_id=chain_id, expected_previous=expected_previous)
        )
    return views


def _chain_view(
    chain_id: str, head_sequence: int, head_view: LedgerEntryView | None
) -> LedgerChainView:
    entry_count = max(0, head_sequence + 1)
    signed_count = entry_count if head_view and head_view.signature_count else 0
    summary_entries: tuple[LedgerEntryView, ...] = ()
    if head_view is not None:
        summary_entries = (
            LedgerEntryView(
                **{
                    **head_view.__dict__,
                    "source": "council-api",
                    "sequence": entry_count - 1,
                    "entry_type": "__summary__",
                    "payload": {"entry_count": entry_count},
                    "metadata": {"signed_count": signed_count},
                }
            ),
        )
    return LedgerChainView(
        chain_id=chain_id,
        source="council-api",
        entries=summary_entries,
        is_valid=head_view.hash_valid if head_view else True,
        head_hash=head_view.content_hash if head_view else "",
        head_sequence=head_sequence,
        broken_at=None if (head_view is None or head_view.hash_valid) else head_sequence,
    )


async def load_ledger_page_from_api(
    client: httpx.AsyncClient,
    api_url: str,
    *,
    limit: int = DEFAULT_PAGE_LIMIT,
    cursor: str = "",
    chain_id: str = "",
    entry_type: str = "",
    query: str = "",
    integrity: str = "",
    validate: bool = False,
) -> dict[str, Any]:
    """Load one ledger page through the council API (same shape as file mode)."""
    limit = max(1, min(limit, MAX_PAGE_LIMIT))
    fetch_limit = FILTER_SCAN_LIMIT if any((entry_type, query, integrity)) else limit
    fetch_limit = min(fetch_limit, 200)  # council-side page cap

    try:
        before = _decode_cursor(cursor)
        # Resolve the chain head first so cursors stay consistent.
        head_page = await _call_tool(
            client, api_url, "council_ledger_page", {"cursor": None, "limit": 1}
        )
        if head_page.get("error"):
            raise ValueError(str(head_page["error"]))
        council_chain_id = str(head_page.get("chain_id") or "")
        head_sequence = int(head_page.get("head_sequence", -1))
        head_raw = (head_page.get("entries") or [None])[0]

        if chain_id and council_chain_id != chain_id:
            return _empty_page(limit, validate, error="")

        head_view = (
            _entry_from_raw(head_raw, chain_id=council_chain_id, expected_previous=None)
            if head_raw
            else None
        )
        chain = _chain_view(council_chain_id, head_sequence, head_view)

        before_seq = before.get(council_chain_id, head_sequence + 1)
        page_views: list[LedgerEntryView] = []
        next_cursor_seq: int | None = None
        if head_sequence >= 0 and before_seq > 0:
            page = await _call_tool(
                client,
                api_url,
                "council_ledger_page",
                {"cursor": before_seq - 1, "limit": fetch_limit},
            )
            if page.get("error"):
                raise ValueError(str(page["error"]))
            raw_entries = list(page.get("entries") or [])
            views = _views_from_page(raw_entries, council_chain_id)
            for view in views:
                if _entry_matches(view, entry_type=entry_type, query=query, integrity=integrity):
                    page_views.append(view)
                    if len(page_views) >= limit:
                        break
            raw_next = page.get("next_cursor")
            if page_views:
                next_cursor_seq = page_views[-1].sequence
            elif raw_next is not None:
                next_cursor_seq = int(raw_next) + 1

        has_more = bool(page_views) and (next_cursor_seq or 0) > 0
        next_before = dict(before)
        if next_cursor_seq is not None:
            next_before[council_chain_id] = next_cursor_seq

        entry_count = max(0, head_sequence + 1)
        signed = chain.summary().get("signed_count", 0)
        return {
            "chains": [chain.summary()],
            "entries": [view.summary() for view in page_views],
            "next_cursor": _encode_cursor(next_before) if has_more else "",
            "has_more": has_more,
            "page_limit": limit,
            "validation_mode": "full" if validate else "fast",
            "summary": {
                "chain_count": 1,
                "entry_count": entry_count,
                "valid_chain_count": 1 if chain.is_valid else 0,
                "invalid_chain_count": 0 if chain.is_valid else 1,
                "signed_entry_count": signed,
                "unsigned_entry_count": max(0, entry_count - signed),
            },
        }
    except Exception as exc:
        logger.warning("council ledger API unavailable: %s", exc)
        return _empty_page(limit, validate, error="council ledger API unavailable")


async def get_ledger_entry_from_api(
    client: httpx.AsyncClient,
    api_url: str,
    *,
    chain_id: str,
    sequence: int,
) -> dict[str, Any] | None:
    """Fetch one entry (with its successor for link verification) via the API."""
    try:
        page = await _call_tool(
            client,
            api_url,
            "council_ledger_page",
            {"cursor": sequence + 1, "limit": 2},
        )
        if page.get("error"):
            return None
        council_chain_id = str(page.get("chain_id") or "")
        if chain_id and council_chain_id != chain_id:
            return None
        raw_entries = list(page.get("entries") or [])
        views = _views_from_page(raw_entries, council_chain_id)
        for view in views:
            if view.sequence == sequence:
                return view.detail()
        return None
    except Exception as exc:
        logger.warning("council ledger API unavailable: %s", exc)
        return None


def _empty_page(limit: int, validate: bool, *, error: str) -> dict[str, Any]:
    page: dict[str, Any] = {
        "chains": [],
        "entries": [],
        "next_cursor": "",
        "has_more": False,
        "page_limit": limit,
        "validation_mode": "full" if validate else "fast",
        "summary": {
            "chain_count": 0,
            "entry_count": 0,
            "valid_chain_count": 0,
            "invalid_chain_count": 0,
            "signed_entry_count": 0,
            "unsigned_entry_count": 0,
        },
    }
    if error:
        page["error"] = error
    return page
