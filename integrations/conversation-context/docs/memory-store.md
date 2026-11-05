<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# How the context service stores and expires records

The context service keeps immutable snapshots and tool events in one process's
memory. A lookup returns the latest accepted snapshot for a sandbox, provided
it is still live, and up to 20 of that sandbox's newest live tool events. Those
events can belong to earlier snapshots. The store does not identify which tool caused an HTTP request, and
it does not isolate tenants or share memory across server replicas.

This document describes the implementation in
[`store.py`](../src/nomos_context_service/store.py). The API endpoints in
[`server.py`](../src/nomos_context_service/server.py) validate and authenticate
each request before calling `publish()` or `lookup()`.

## The data kept in memory

The primary dictionary, `_entries`, owns each immutable `Entry`. Its key is:

```python
(kind, sandbox_name, record_id)
```

`kind` is `snapshot` or `tool`. For a snapshot, `record_id` is its snapshot ID.
For a tool event, it is the event's record ID. A pending event and its later
completion have different record IDs, even though they share a `tool_call_id`.

An `Entry` contains the canonical JSON payload as `raw` bytes, its parent
`snapshot_id`, its service-assigned `revision`, its acceptance time and two
expiration deadlines. Revisions increase for every newly accepted record. They
determine event ordering; the publisher's optional `recorded_at` does not.

The store adds indexes so each lookup need not scan all entries or sort events:

| Structure | What it contains | Why it exists |
| --- | --- | --- |
| `_entries` | `(kind, sandbox, ID)` → immutable `Entry` | Direct record access, parent validation and duplicate detection |
| `_tools_by_sandbox` | Sandbox → `OrderedDict` of event ID → the same `Entry` | Retrieve one sandbox's newest events directly |
| `_latest` | Sandbox → latest accepted snapshot ID | Find its current retained user context |
| `_live_counts` | Sandbox → number of live snapshots and events | Remove empty-sandbox metadata without scanning all entries |
| `_monotonic_expiry` | Min-heap of monotonic deadlines, revisions and record keys | Expire entries after elapsed time, even if the calendar clock moves backward |
| `_wall_expiry` | Min-heap of UTC deadlines, revisions and record keys | Expire entries when the absolute deadline is reached, including a forward clock correction |

The event index points to the same `Entry` objects as the primary dictionary;
it does not copy their JSON bytes. Heap items contain deadlines, revisions and
keys, never payload bytes. Decoding content for a lookup response still creates
separate response objects.

For example, the following is a schematic view of the dictionaries. The
`entry_*` names represent the same objects in both indexes:

```python
_entries = {
    ("snapshot", "hermes-a", "S1"): entry_S1,
    ("tool", "hermes-a", "event-A-completed"): entry_A,
    ("snapshot", "hermes-a", "S2"): entry_S2,
    ("tool", "hermes-a", "event-B-pending"): entry_B,
    ("tool", "hermes-b", "event-C-completed"): entry_C,
    # hermes-b's parent snapshot is omitted from this illustration.
}

_tools_by_sandbox = {
    "hermes-a": OrderedDict([
        ("event-A-completed", entry_A),  # older acceptance
        ("event-B-pending", entry_B),    # newer acceptance
    ]),
    "hermes-b": OrderedDict([
        ("event-C-completed", entry_C),
    ]),
}

_latest["hermes-a"] = "S2"
```

A lookup for `hermes-a` retrieves B then A without examining C. It returns
S2's retained user context. It does not retrieve S1's user text or require A's
parent snapshot to equal S2. Parent IDs remain attached to the events.

## What happens when a record arrives?

`POST /v1/context-snapshots` and `POST /v1/tool-records` call `publish()` after
request validation. Canonical JSON encoding happens before taking the store
lock. The following checks and updates happen together under that lock:

1. Purge records whose deadlines have passed.
2. For a snapshot, validate its fixed `not_after` deadline. For a tool event,
   require its parent snapshot in the same sandbox and inherit its deadlines.
3. Check for an existing record with this key. An identical retry returns the
   original receipt. Different content under a live existing key returns `409`.
4. Check the total record and canonical-byte limits. A full store returns `507`;
   it does not evict live records to make space.
5. Assign a new revision and insert the `Entry` into `_entries`. Update the
   sandbox's live count and either its latest-snapshot pointer or tool index.
6. Push the new record's deadlines into both heaps and account for its bytes.

Identical retries do not append to the event index, add heap items, change
ordering, renew expiry or change which snapshot is latest. Validation and
capacity errors do not insert a record or its indexes.

## What a lookup reads

`POST /v1/context-lookups` calls `lookup(sandbox_name, include_content=...)`.
While holding the same lock, the store purges due records, finds the sandbox's
latest snapshot, and selects from its ordered event index:

```python
tools = self._tools_by_sandbox.get(sandbox_name)
selected = (
    list(islice(reversed(tools.values()), MAX_LOOKUP_TOOL_RECORDS))
    if tools else []
)
```

`OrderedDict` preserves acceptance order. Reversing its values starts at the
newest event; `islice(..., 20)` stops after 20. The index's length determines
`tool_records_truncated`, without sorting or scanning the rest of the store.

The response includes the latest snapshot reference, selected event references
and expiry metadata. With `include_content=true`, it also decodes the snapshot
and event JSON bytes. With the default false value, the content is omitted.
The OpenShell reader can subsequently omit more whole events to satisfy its
own context budget; the service's 20-event limit is a separate limit.

## How expiry works

The publisher chooses a fixed UTC `not_after` value for each snapshot. The
service's default maximum future deadline is **600 seconds (ten minutes)**.
The Relay publisher still defaults to **240 seconds (four minutes)**; increasing
the ceiling does not change that default. A tool event inherits its parent's
expiry, rather than receiving a new lifetime when the event arrives.

For example:

```text
12:00:00  S1 accepted, with not_after = 12:04:00
12:03:30  Event A accepted under S1; A also expires at 12:04:00
12:04:00  S1 and A become unavailable
```

Publication after a missing or expired parent is rejected with
`404 snapshot_unavailable`. Reading, retrying or publishing a newer snapshot
does not extend the older parent's deadline. The service does not keep active
tools' parents alive automatically.

### Why two clocks?

The store samples wall time and monotonic time when accepting a snapshot. If
the UTC deadline is 240 seconds away, it also records a monotonic deadline
240 seconds ahead. Monotonic time measures elapsed time and is not adjusted
when the calendar clock is corrected.

A record expires when **either** deadline is reached:

```text
Wall clock moves backward → monotonic expiry still closes access on time
Wall clock moves forward past not_after → wall expiry closes access immediately
```

Tool events copy both deadlines from their parent. These deadlines do not use
the publisher's `recorded_at`, and they do not establish when a tool executed.

### What is a heap, and what do heappush and heappop do?

A min-heap is an array organized so its smallest item is at position zero. Here,
"smallest" means the earliest deadline. Each heap item has this structure:

```python
(deadline, revision, record_key)
```

The revision breaks ties between equal deadlines and identifies the exact
publication. A heap is not a completely sorted array: only the earliest item
must be accessible at the front. For example, this is a valid heap shape:

```text
             110: event-B
              /        \
     150: event-C     120: event-A
```

Python's `heappush()` inserts an item and restores the heap ordering;
`heappop()` removes the earliest item and restores that ordering. At time 125,
cleanup pops B, then A, then stops because C's deadline is still in the future.
It does not need to inspect every later deadline.

`_purge()` does this independently for each clock's heap:

```python
while heap and heap[0][0] <= now:
    _, revision, key = heappop(heap)
    self._remove(key, revision)
```

`heappush()` itself does not run a timer or delete anything. The service calls
`_purge()` before each publication and lookup. Its existing background task
also calls `purge()` every 60 seconds to clean up idle stores. Thus expired
content can remain allocated until cleanup runs, but a read or write always
checks expiry before accessing it.

### Removing records safely and bounding stale heap items

When one clock expires a record, `_remove()` first verifies that `_entries`
still contains that key **with the heap item's revision**. It then removes the
entry, subtracts its canonical-byte size, updates its event index and decrements
its sandbox's live count. The record's item in the other heap may remain until
its deadline is reached. It holds a key and revision, not the expired payload.

The revision check matters if an expired key is reused for a new publication.
The old heap item must not delete the new entry. It also makes encountering
the same expired publication in the other heap harmless: removal and byte
accounting happen only once.

Stale items must not grow forever when only one clock advances. After purging,
if either heap exceeds **twice the live record count plus 64 items**, the store
rebuilds both heaps from the live entries and calls `heapify()`. This is an
occasional full-store pass that bounds index growth. If no entries remain, both
heaps are cleared immediately. Rebuilding indexes does not renew any deadline.

### Why an expired latest snapshot does not reveal an older one

Suppose S1 expires at 12:10, then newer S2 is accepted with expiry 12:05. At
12:06, S1 remains stored, but lookup must not silently switch back to S1.

`_latest` therefore continues to point to S2 while older records for that sandbox
remain live. Lookup returns `404 context_unavailable` because S2 is gone. The
pointer is a fence against falling back to an older snapshot. `_live_counts`
lets cleanup retain that fence without scanning all entries; once the sandbox
has no records left, its count and latest pointer are removed. A genuinely new
snapshot can establish a new latest view.

## Cost, locking and limits

Ordinary lookups with nothing due for expiry inspect two heap heads and at most
20 indexed events, plus the returned JSON if content is requested. They no
longer scan every record or sort all of a sandbox's events. Each new publication
adds two heap items, each with logarithmic insertion cost.

This does not make every operation constant time. Large expiry batches process
all due heap items; heap insertion/removal is logarithmic; occasional heap
compaction is linear in the number of live records. All of this, including
response construction, still runs under one lock. The optimization removes
routine scans without adding background threads or concurrent index mutation.
No measured latency improvement is claimed here.

The defaults remain 1,000 total records and 16 MiB of canonical payload bytes.
Indexes, heap metadata, decoded responses and Python overhead are additional
memory, so the payload limit is not a process-RSS limit. Nothing in this store
is persisted to disk, Redis, PostgreSQL or the Council ledger. Restart loses
the data. Expiry does not guarantee cryptographic erasure from RAM or swap.

## How this is verified

[`test_store.py`](../tests/test_store.py) checks indexed lookup without global
iteration, retry ordering, sandbox isolation, independent clock expiry, reused
keys with old heap items, bounded metadata under one-clock churn, latest-pointer
fences and concurrent publication ordering. The existing API/CLI suite continues
to cover authentication, fixed deadlines, byte/count capacity, immutable writes,
truncation metadata and periodic cleanup.
