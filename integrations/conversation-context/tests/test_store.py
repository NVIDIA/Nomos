# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Exercise indexed lookup, clock-independent expiry and bounded stale metadata."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from nomos_context_service.models import SnapshotInput, ToolRecordInput
from nomos_context_service.store import ContextStore, StoreError


@dataclass
class Clock:
    """Advance either clock without sleeping or depending on actual calendar time."""

    now: float

    def __call__(self):
        return self.now


@pytest.fixture
def clocks():
    return Clock(1000), Clock(2_000_000_000)


def snapshot(wall, record_id="snapshot-1", *, sandbox="sandbox-a", lifetime=600):
    """Use a fixed future deadline relative to the test's independent wall clock."""
    return SnapshotInput(
        sandbox_name=sandbox,
        snapshot_id=record_id,
        model_attempt_id="attempt-" + record_id,
        model_name="fixture-model",
        not_after=datetime.fromtimestamp(wall.now + lifetime, UTC),
        context={"latest_user_request": "Read the fictional progress notes."},
    )


def tool(record_id="event-1", *, parent="snapshot-1", sandbox="sandbox-a"):
    return ToolRecordInput(
        sandbox_name=sandbox,
        record_id=record_id,
        snapshot_id=parent,
        tool_call_id="call-" + record_id,
        tool_name="terminal",
        state="completed",
        arguments={"command": "cat /workspace/progress.txt"},
        arguments_incomplete=False,
    )


def assert_indexes_match(store):
    """Check all indexes against an independent scan of authoritative live entries."""
    assert store._bytes == sum(len(entry.raw) for entry in store._entries.values())
    assert store._retained_counts == Counter(e.sandbox_name for e in store._entries.values())
    expected_tools = {}
    for entry in sorted(store._entries.values(), key=lambda e: e.revision):
        if entry.kind == "tool":
            expected_tools.setdefault(entry.sandbox_name, []).append(entry)
    assert set(store._tools_by_sandbox) == set(expected_tools)
    for name, expected in expected_tools.items():
        assert list(store._tools_by_sandbox[name].values()) == expected
        for entry in expected:
            assert (
                store._tools_by_sandbox[name][entry.record_id]
                is store._entries[("tool", name, entry.record_id)]
            )
    assert set(store._latest) <= set(store._retained_counts)
    assert all(store._entries[key].revision == rev for key, rev in store._observed_expired.items())


class NoScanDict(dict):
    """Fail if an ordinary lookup tries to scan all records for selection or expiry."""

    def values(self):
        raise AssertionError("ordinary lookup scanned all values")

    def items(self):
        raise AssertionError("ordinary lookup scanned all items")

    def __iter__(self):
        raise AssertionError("ordinary lookup scanned all keys")


def test_lookup_and_duplicate_publication_do_not_scan_or_reorder(clocks):
    mono, wall = clocks
    store = ContextStore(clock=mono, wall_clock=wall)
    for name in ("sandbox-a", "sandbox-b"):
        store.publish(snapshot(wall, sandbox=name))
        for i in range(25):
            store.publish(tool(f"event-{i}", sandbox=name))
    before = len(store._monotonic_expiry), len(store._wall_expiry), store._revision
    store._entries = NoScanDict(store._entries)
    assert store.publish(tool("event-0"))["duplicate"] is True
    result = store.lookup("sandbox-a", include_content=True)
    assert result["recent_tool_record_refs"] == [f"event-{i}" for i in range(24, 4, -1)]
    assert result["tool_records_truncated"] is True
    assert all(e["sandbox_name"] == "sandbox-a" for e in result["tool_records"])
    assert before == (len(store._monotonic_expiry), len(store._wall_expiry), store._revision)


@pytest.mark.parametrize("advancing_clock", ["monotonic", "wall"])
def test_either_clock_removes_every_index_and_recovers_capacity(clocks, advancing_clock):
    mono, wall = clocks
    store = ContextStore(clock=mono, wall_clock=wall, max_records=2)
    store.publish(snapshot(wall))
    store.publish(tool())
    (mono if advancing_clock == "monotonic" else wall).now += 600
    store.purge()
    assert_indexes_match(store)
    assert not store._entries and not store._latest
    assert not store._monotonic_expiry and not store._wall_expiry
    assert store.publish(snapshot(wall))["duplicate"] is False
    assert_indexes_match(store)


@pytest.mark.parametrize("first_clock", ["monotonic", "wall"])
def test_old_heap_item_cannot_delete_a_reused_record_key(clocks, first_clock):
    mono, wall = clocks
    store = ContextStore(clock=mono, wall_clock=wall)
    store.publish(snapshot(wall, "anchor"))
    store.publish(snapshot(wall, lifetime=10))
    old = store.publish(tool())
    first, second = (mono, wall) if first_clock == "monotonic" else (wall, mono)
    first.now += 11
    store.purge()
    other_heap = store._wall_expiry if first_clock == "monotonic" else store._monotonic_expiry
    assert any(item[1] == old["revision"] for item in other_heap)
    store.publish(snapshot(wall, lifetime=100))
    new = store.publish(tool())
    second.now += 11
    store.purge()
    result = store.lookup("sandbox-a", include_content=True)
    assert result["recent_tool_record_refs"] == ["event-1"]
    assert store._entries[("tool", "sandbox-a", "event-1")].revision == new["revision"]
    assert_indexes_match(store)


@pytest.mark.parametrize("advancing_clock", ["monotonic", "wall"])
def test_one_clock_churn_compacts_stale_metadata(clocks, advancing_clock):
    mono, wall = clocks
    store = ContextStore(clock=mono, wall_clock=wall)
    store.publish(snapshot(wall, "anchor"))
    heap_name = "_wall_expiry" if advancing_clock == "monotonic" else "_monotonic_expiry"
    previous_size, compacted = 0, False
    for _ in range(128):
        store.publish(snapshot(wall, lifetime=1))
        store.publish(tool())
        size = len(getattr(store, heap_name))
        compacted |= size < previous_size
        previous_size = size
        # An intentionally loose bound avoids coupling the test to the exact slack.
        assert size <= 4 * len(store._entries) + 128
        assert_indexes_match(store)
        (mono if advancing_clock == "monotonic" else wall).now += 1
        store.purge()
    assert compacted
    mono.now += 600
    wall.now += 600
    store.purge()
    assert_indexes_match(store)
    assert not store._entries and not store._latest
    assert not store._monotonic_expiry and not store._wall_expiry


def test_expiry_keeps_newest_window_across_parents_and_latest_fence(clocks):
    mono, wall = clocks
    store = ContextStore(clock=mono, wall_clock=wall)
    store.publish(snapshot(wall))
    for i in range(25):
        store.publish(tool(f"old-{i}"))
    store.publish(snapshot(wall, "short", lifetime=5))
    for i in range(5):
        store.publish(tool(f"short-{i}", parent="short"))
    assert store.lookup("sandbox-a")["recent_tool_record_refs"][:5] == [
        f"short-{i}" for i in range(4, -1, -1)
    ]
    mono.now += 5
    with pytest.raises(StoreError, match="context_unavailable"):
        store.lookup("sandbox-a")
    assert store._latest["sandbox-a"] == "short"
    assert_indexes_match(store)
    store.publish(snapshot(wall, "new"))
    result = store.lookup("sandbox-a")
    assert result["recent_tool_record_refs"] == [f"old-{i}" for i in range(24, 4, -1)]
    assert result["tool_records_truncated"] is True


def test_rejected_publications_leave_live_indexes_unchanged(clocks):
    mono, wall = clocks
    store = ContextStore(clock=mono, wall_clock=wall, max_records=2)
    store.publish(snapshot(wall))
    record = tool()
    store.publish(record)
    before = dict(store._entries), list(store._monotonic_expiry), list(store._wall_expiry)
    revision = store._revision
    with pytest.raises(StoreError, match="store_capacity_reached"):
        store.publish(tool("extra"))
    conflict = record.model_copy(update={"arguments": {"command": "cat /workspace/other.txt"}})
    with pytest.raises(StoreError, match="record_id_conflict"):
        store.publish(conflict)
    assert before == (store._entries, store._monotonic_expiry, store._wall_expiry)
    assert store._revision == revision
    assert_indexes_match(store)


def test_requests_do_not_run_heap_cleanup_even_when_records_expire(clocks, monkeypatch):
    """Requests hide expired content and reject parents without reclaiming storage."""
    mono, wall = clocks
    store = ContextStore(clock=mono, wall_clock=wall)
    old = snapshot(wall, lifetime=5)
    store.publish(old)
    store.publish(tool())
    store.publish(snapshot(wall, "new"))
    mono.now += 5

    def unexpected_cleanup(*_):
        raise AssertionError("request ran heap cleanup")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_purge", unexpected_cleanup)
        store.publish(tool("new-event", parent="new"))
        result = store.lookup("sandbox-a", include_content=True)
        assert result["recent_tool_record_refs"] == ["new-event"]
        assert result["tool_records_truncated"] is False
        with pytest.raises(StoreError, match="snapshot_unavailable"):
            store.publish(tool("late-event"))
        with pytest.raises(StoreError, match="publication_expired"):
            store.publish(old)
        # The bytes and indexes still retain the expired objects until the timer runs.
        assert len(store._entries) == 4
        assert len(store._observed_expired) == 2
        assert_indexes_match(store)
    store.purge()
    assert len(store._entries) == 2
    assert not store._observed_expired
    assert_indexes_match(store)


@pytest.mark.parametrize("expired_kind", ["snapshot", "tool"])
def test_observed_expiry_cannot_reverse_before_cleanup(clocks, expired_kind):
    mono, wall = clocks
    store = ContextStore(clock=mono, wall_clock=wall)
    store.publish(snapshot(wall, lifetime=5))
    if expired_kind == "tool":
        store.publish(tool())
        store.publish(snapshot(wall, "new"))
    wall.now += 5
    if expired_kind == "snapshot":
        with pytest.raises(StoreError, match="context_unavailable"):
            store.lookup("sandbox-a")
    else:
        assert store.lookup("sandbox-a")["recent_tool_record_refs"] == []
    wall.now -= 5
    if expired_kind == "snapshot":
        with pytest.raises(StoreError, match="context_unavailable"):
            store.lookup("sandbox-a")
    else:
        assert store.lookup("sandbox-a")["recent_tool_record_refs"] == []
    store.purge()
    key = (expired_kind, "sandbox-a", "snapshot-1" if expired_kind == "snapshot" else "event-1")
    assert key not in store._entries
    assert not store._observed_expired
    assert_indexes_match(store)


@pytest.mark.parametrize("live_events", [2, 25])
def test_live_event_window_skips_expired_entries_waiting_for_cleanup(clocks, live_events):
    mono, wall = clocks
    store = ContextStore(clock=mono, wall_clock=wall)
    store.publish(snapshot(wall))
    for i in range(live_events):
        store.publish(tool(f"live-{i}"))
    store.publish(snapshot(wall, "short", lifetime=5))
    for i in range(25):
        store.publish(tool(f"expired-{i}", parent="short"))
    store.publish(snapshot(wall, "new"))
    mono.now += 5
    result = store.lookup("sandbox-a")
    assert result["recent_tool_record_refs"] == [
        f"live-{i}" for i in range(live_events - 1, max(-1, live_events - 21), -1)
    ]
    assert result["tool_records_truncated"] is (live_events > 20)
    assert len(store._observed_expired) == 26  # The 25 events and their expired parent.
    assert_indexes_match(store)


def test_observed_parent_expiry_hides_events_after_rollback_and_id_reuse(clocks):
    mono, wall = clocks
    store = ContextStore(clock=mono, wall_clock=wall)
    store.publish(snapshot(wall, lifetime=5))
    store.publish(tool())
    store.publish(snapshot(wall, "new"))
    wall.now += 5
    with pytest.raises(StoreError, match="snapshot_unavailable"):
        store.publish(tool("late-event"))
    wall.now -= 5
    # The parent was observed expired even though this child was never accessed.
    assert store.lookup("sandbox-a")["recent_tool_record_refs"] == []
    store.purge()
    assert_indexes_match(store)
    store.publish(snapshot(wall))
    assert store.lookup("sandbox-a")["recent_tool_record_refs"] == []


def test_reused_parent_id_cannot_validate_unobserved_old_child(clocks):
    mono, wall = clocks
    store = ContextStore(clock=mono, wall_clock=wall)
    store.publish(snapshot(wall, lifetime=5))
    store.publish(tool())
    wall.now += 5
    with pytest.raises(StoreError, match="context_unavailable"):
        store.lookup("sandbox-a")
    wall.now -= 5
    store.purge()  # Removes the observed parent, not its unobserved child's future heap item.
    assert ("tool", "sandbox-a", "event-1") in store._entries
    store.publish(snapshot(wall))
    assert store.lookup("sandbox-a")["recent_tool_record_refs"] == []
    store.purge()
    assert_indexes_match(store)


def test_expired_snapshot_retry_records_expiry_before_rejecting_deadline(clocks):
    mono, wall = clocks
    store = ContextStore(clock=mono, wall_clock=wall)
    record = snapshot(wall, lifetime=5)
    store.publish(record)
    wall.now += 5
    with pytest.raises(StoreError, match="publication_expired"):
        store.publish(record)
    wall.now -= 5
    with pytest.raises(StoreError, match="context_unavailable"):
        store.lookup("sandbox-a")
    store.purge()
    assert not store._entries


def test_concurrent_publications_keep_revision_order_and_consistent_indexes(clocks):
    mono, wall = clocks
    store = ContextStore(clock=mono, wall_clock=wall)
    store.publish(snapshot(wall))

    def publish_and_lookup(i):
        record = tool(f"event-{i}")
        receipt = store.publish(record)
        store.lookup("sandbox-a", include_content=True)
        return receipt

    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(publish_and_lookup, range(64)))
    expected = sorted(receipts, key=lambda r: r["revision"], reverse=True)[:20]
    assert store.lookup("sandbox-a")["recent_tool_record_refs"] == [
        receipt["record_id"] for receipt in expected
    ]
    assert_indexes_match(store)
