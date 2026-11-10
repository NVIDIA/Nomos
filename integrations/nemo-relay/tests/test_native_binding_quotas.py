# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Response identity publication has its own caps and preserves in-flight result space."""

from contextlib import contextmanager
from dataclasses import replace

import pytest

from nemo_relay_nomos.repository import EvidenceStore, StoreError
from nemo_relay_nomos.settings import Limits


@contextmanager
def store_with(**overrides):
    """Give each quota scenario a fresh in-memory store and close it on failure too."""
    store = EvidenceStore(("test-agent", "test-council", "test-activation"), replace(Limits(), **overrides))
    try:
        yield store
    finally:
        store.close()


def test_binding_publication_ignores_a_full_completed_journal():
    """Journal capacity limits guarded admission, not response identity publication."""
    with store_with(max_invocations=1, conversation_share_percent=100) as store:
        origin = store.open_native_turn("agent", "request")
        store.begin("agent", "completed", "hash")
        store.finish("agent", "completed", {"status": "executed"})
        store.bind_native_calls(origin, [("new-proposal", "read_file")])
        assert store.native_turn("agent", "new-proposal") == origin
        with pytest.raises(StoreError, match="storage_quota_exceeded"):
            store.begin("agent", "new-proposal", "hash")


def test_binding_publication_does_not_reserve_a_future_guarded_result():
    """An unguarded proposal needs only its binding bytes, not guarded-execution headroom."""
    with store_with(max_storage_bytes=1024, conversation_share_percent=100) as store:
        origin = store.open_native_turn("agent", "request")
        store.bind_native_calls(origin, [("proposal", "ordinary_tool")])
        assert store.native_turn("agent", "proposal") == origin
        with pytest.raises(StoreError, match="storage_quota_exceeded"):
            store.begin("agent", "proposal", "hash")


def test_binding_publication_preserves_space_reserved_for_in_flight_results():
    """Binding metadata cannot consume bytes already promised to an admitted call."""
    with store_with(max_storage_bytes=Limits().reserve_bytes + 1500, conversation_share_percent=100) as store:
        origin = store.open_native_turn("agent", "request")
        store.begin("agent", "in-flight", "hash")
        with pytest.raises(StoreError, match="storage_quota_exceeded"):
            store.bind_native_calls(origin, [("proposal", "read_file")])
        assert store.native_turn("agent", "proposal") is None
        store.finish("agent", "in-flight", {"status": "executed"})
        store.bind_native_calls(origin, [("proposal", "read_file")])
        assert store.native_turn("agent", "proposal") == origin


@pytest.mark.parametrize("share", [50, 100])
def test_binding_bytes_remain_bounded_globally_and_per_agent(share):
    """An Agent cannot publish identity metadata past either storage-byte budget."""
    with store_with(max_storage_bytes=1024 * 100 // share, conversation_share_percent=share) as store:
        origin = store.open_native_turn("agent", "request")
        store.bind_native_calls(origin, [("first", "read_file")])
        with pytest.raises(StoreError, match="storage_quota_exceeded"):
            store.bind_native_calls(origin, [("second", "read_file")])
        assert store.native_turn("agent", "first") == origin
        assert store.native_turn("agent", "second") is None


def test_binding_counts_remain_bounded_globally_and_per_agent():
    """Publishing IDs still enforces dedicated count caps independently of the journal."""
    with store_with(max_invocations=2, conversation_share_percent=50) as store:
        first = store.open_native_turn("first-agent", "request")
        second = store.open_native_turn("second-agent", "request")
        with pytest.raises(StoreError, match="native_binding_quota"):
            store.bind_native_calls(first, [("a", "tool"), ("b", "tool")])
        store.bind_native_calls(first, [("a", "tool")])
        store.bind_native_calls(second, [("a", "tool")])
        with pytest.raises(StoreError, match="native_binding_quota"):
            store.bind_native_calls(second, [("b", "tool")])
        assert store.native_turn("first-agent", "a") == first
        assert store.native_turn("second-agent", "a") == second


def test_expired_origin_still_rejects_tool_proposals(monkeypatch):
    """Only text-only responses bypass expiry; actual proposals still require a live origin."""
    clock = [1000.0]
    monkeypatch.setattr("nemo_relay_nomos.repository.time.time", lambda: clock[0])
    with store_with(retention_seconds=60) as store:
        origin = store.open_native_turn("agent", "request")
        clock[0] += 61
        with pytest.raises(StoreError, match="native_context_expired"):
            store.bind_native_calls(origin, [("proposal", "read_file")])
        assert store.native_turn("agent", "proposal") is None
