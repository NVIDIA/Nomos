# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

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
import os
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
    ledger_validation,
)

logger = logging.getLogger(__name__)

COUNCIL_PAGE_LIMIT = 200  # council_ledger_page's own cap
# A full check walks the chain one council page at a time. Past this many
# entries it stops and reports how far it got instead of holding the request.
MAX_VALIDATED_ENTRIES = 20_000


async def _call_tool(
    client: httpx.AsyncClient,
    api_url: str,
    tool: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Invoke a council tool via the NAT debug endpoint and decode its result."""
    url = f"{api_url.rstrip('/')}/debug/tools/call/{tool}"
    # A read-only MCP bridge can provide this HTTP contract when the NAT
    # frontend exposes tools only over MCP. Never reuse management credentials.
    token = os.environ.get("MONITOR_LEDGER_API_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = await client.post(url, json=payload, headers=headers, timeout=10.0)
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
                    "metadata": {},
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


async def _validate_chain(
    client: httpx.AsyncClient,
    api_url: str,
    *,
    council_id: str,
    chain_id: str,
    head_sequence: int,
) -> dict[str, Any]:
    """Walk the chain from its head towards genesis, checking every entry.

    Each entry's hash is recomputed, and it must be the hash its successor
    names as `previous_hash`. A sequence the council does not return is a break.
    """
    expected_sequence = head_sequence
    successor_previous: str | None = None
    validated = 0
    signed = 0
    broken_at: int | None = None
    type_counts: dict[str, int] = {}
    while 0 <= expected_sequence and validated < MAX_VALIDATED_ENTRIES:
        page = await _call_tool(
            client,
            api_url,
            "council_ledger_page",
            _tool_args(council_id, cursor=expected_sequence, limit=COUNCIL_PAGE_LIMIT),
        )
        if page.get("error"):
            raise ValueError(str(page["error"]))
        raw_entries = list(page.get("entries") or [])
        if not raw_entries:
            broken_at = expected_sequence
            break
        for raw in raw_entries:
            view = _entry_from_raw(raw, chain_id=chain_id, expected_previous=successor_previous)
            if view.sequence != expected_sequence:
                # Entries between the two are missing.
                broken_at = expected_sequence
                expected_sequence = -1
                break
            # Genesis is exempt from the link check in the shared view; it must
            # still be the entry its successor points back to.
            linked = successor_previous is None or view.content_hash == successor_previous
            if not (view.hash_valid and view.link_valid and linked):
                broken_at = view.sequence
            validated += 1
            signed += 1 if view.signature_count else 0
            type_counts[view.entry_type] = type_counts.get(view.entry_type, 0) + 1
            successor_previous = view.previous_hash
            expected_sequence -= 1
    entry_count = max(0, head_sequence + 1)
    complete = validated == entry_count
    return {
        "is_valid": broken_at is None,
        "broken_at": broken_at,
        "signed_count": signed if complete else None,
        "type_counts": type_counts,
        "fully_validated": complete,
        "validated_entries": validated,
    }


def _tool_args(council_id: str, **args: Any) -> dict[str, Any]:
    """Tool arguments, naming the council only when one was selected."""
    return {**args, "council_id": council_id} if council_id else args


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
    council_id: str = "",
) -> dict[str, Any]:
    """Load one ledger page through the council API (same shape as file mode).

    `council_id` selects whose ledger to read; empty means the council's default.
    """
    limit = max(1, min(limit, MAX_PAGE_LIMIT))
    fetch_limit = FILTER_SCAN_LIMIT if any((entry_type, query, integrity)) else limit
    fetch_limit = min(fetch_limit, COUNCIL_PAGE_LIMIT)

    try:
        before = _decode_cursor(cursor)
        # Resolve the chain head first so cursors stay consistent.
        head_page = await _call_tool(
            client,
            api_url,
            "council_ledger_page",
            _tool_args(council_id, cursor=None, limit=1),
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
                _tool_args(council_id, cursor=before_seq - 1, limit=fetch_limit),
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
        chain_summary = chain.summary()
        if validate and head_view is not None:
            chain_summary.update(
                await _validate_chain(
                    client,
                    api_url,
                    council_id=council_id,
                    chain_id=council_chain_id,
                    head_sequence=head_sequence,
                )
            )
        chains = [chain_summary]
        return {
            "chains": chains,
            "entries": [view.summary() for view in page_views],
            "next_cursor": _encode_cursor(next_before) if has_more else "",
            "has_more": has_more,
            "page_limit": limit,
            **ledger_validation(chains, entry_count, requested=validate),
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
    council_id: str = "",
) -> dict[str, Any] | None:
    """Fetch one entry (with its successor for link verification) via the API."""
    try:
        page = await _call_tool(
            client,
            api_url,
            "council_ledger_page",
            _tool_args(council_id, cursor=sequence + 1, limit=2),
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
        **ledger_validation([], 0, requested=validate),
    }
    if error:
        page["error"] = error
    return page
