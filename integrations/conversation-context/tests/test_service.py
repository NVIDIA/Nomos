# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Exercise authentication, immutable writes, retention and lookup boundaries."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from nomos_context_service.models import SnapshotInput
from nomos_context_service.server import create_app
from nomos_context_service.store import ContextStore

PUBLISHER = "test-only-publisher-credential-00001"
READER = "test-only-reader-credential-0000001"
PUBLISH_HEADERS = {"Authorization": f"Bearer {PUBLISHER}"}
READ_HEADERS = {"Authorization": f"Bearer {READER}"}
WALL_EPOCH = float(int(time.time()))


class Clock:
    """Move monotonic time deterministically without sleeps or wall-clock changes."""

    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


@pytest.fixture
def service():
    """Create one isolated store and authenticated API."""
    clock = Clock()
    store = ContextStore(
        ttl_seconds=300, clock=clock, wall_clock=lambda: WALL_EPOCH + clock.now - 100
    )
    app = create_app(
        publisher_token=PUBLISHER,
        reader_token=READER,
        store=store,
    )
    with TestClient(app) as client:
        yield client, clock


def snapshot(name="hermes-unique-a", snapshot_id="snapshot-1", **overrides):
    """Build a synthetic selected-context payload, without actual conversation data."""
    return {
        "sandbox_name": name,
        "snapshot_id": snapshot_id,
        "model_attempt_id": f"attempt-{snapshot_id}",
        "model_name": "test-model",
        "not_after": datetime.fromtimestamp(WALL_EPOCH + 300, UTC).isoformat(),
        "context": {"messages": [{"role": "user", "content": "Fictional workshop notice."}]},
        **overrides,
    }


def publish(client, body=None):
    """Submit a snapshot with the dedicated publisher credential."""
    return client.post("/v1/context-snapshots", headers=PUBLISH_HEADERS, json=body or snapshot())


def lookup(client, name="hermes-unique-a", **options):
    """Query a single sandbox; the middleware's observed IDs are audit metadata only."""
    return client.post(
        "/v1/context-lookups",
        headers=READ_HEADERS,
        json={"sandbox_name": name, **options},
    )


def tool_record(record_id="record-1", **overrides):
    """Represent one immutable execution event linked to a prior snapshot."""
    return {
        "sandbox_name": "hermes-unique-a",
        "record_id": record_id,
        "snapshot_id": "snapshot-1",
        "tool_call_id": "call-1",
        "tool_name": "terminal",
        "state": "execution_pending",
        "arguments": {"command": "printf fictional"},
        **overrides,
    }


def test_metadata_lookup_omits_context_and_tool_arguments(service):
    """The default response provides association references, not conversation content."""
    client, _ = service
    assert publish(client).status_code == 201
    assert (
        client.post("/v1/tool-records", headers=PUBLISH_HEADERS, json=tool_record()).status_code
        == 201
    )
    result = lookup(client, observed_sandbox_id="sandbox-123", http_request_id="http-1")
    assert result.status_code == 200
    data = result.json()
    assert data["sandbox_name"] == "hermes-unique-a"
    assert data["snapshot_ref"] == "snapshot-1"
    assert data["recent_tool_record_refs"] == ["record-1"]
    assert data["exact_tool_call_id_for_http_request"] is None
    assert data["source_content_omitted"] is True
    assert "Fictional workshop" not in result.text
    assert "printf fictional" not in result.text


def test_content_read_requires_an_explicit_request(service):
    """Only an explicitly requested content view includes selected history and tools."""
    client, _ = service
    publish(client)
    client.post("/v1/tool-records", headers=PUBLISH_HEADERS, json=tool_record())
    data = lookup(client, include_content=True).json()
    assert data["snapshot"]["context"] == snapshot()["context"]
    assert data["tool_records"][0]["arguments"] == tool_record()["arguments"]
    assert data["content_trust"] == "agent_reported_workload_mutable"


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, READ_HEADERS])
def test_only_publisher_can_write(service, headers):
    """Missing, invalid and read-only credentials cannot store snapshots."""
    client, _ = service
    assert client.post("/v1/context-snapshots", headers=headers, json=snapshot()).status_code == 401
    assert lookup(client).status_code == 404


def test_publisher_cannot_read_and_reader_cannot_publish_tools(service):
    """Separate opaque tokens define separate API capabilities."""
    client, _ = service
    publish(client)
    assert (
        client.post(
            "/v1/context-lookups", headers=PUBLISH_HEADERS, json={"sandbox_name": "hermes-unique-a"}
        ).status_code
        == 401
    )
    assert (
        client.post("/v1/tool-records", headers=READ_HEADERS, json=tool_record()).status_code == 401
    )


def test_unauthed_request_is_rejected_before_json_parsing(service):
    """Bad credentials must not trigger parsing of arbitrary payloads."""
    client, _ = service
    response = client.post("/v1/context-snapshots", content=b"not-json")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_snapshots_are_idempotent_without_renewing_expiry(service):
    """Identical retries retain their revision and original server expiry."""
    client, clock = service
    first = publish(client).json()
    clock.now += 250
    retry = publish(client)
    assert retry.status_code == 200
    assert retry.json()["duplicate"] is True
    assert retry.json()["revision"] == first["revision"]
    assert retry.json()["expires_at"] == first["expires_at"]
    clock.now += 50
    assert lookup(client).status_code == 404
    assert publish(client).status_code == 410


def test_conflicting_retry_cannot_change_stored_content(service):
    """An ID is immutable even when new content uses the same sandbox key."""
    client, _ = service
    publish(client)
    assert publish(client, snapshot(context={"changed": True})).status_code == 409
    assert (
        lookup(client, include_content=True).json()["snapshot"]["context"] == snapshot()["context"]
    )


def test_new_snapshot_becomes_latest_without_extending_older_records(service):
    """Newest writes are name-local and do not reset previous snapshot retention."""
    client, clock = service
    first = publish(client).json()
    clock.now += 250
    second = publish(
        client,
        snapshot(
            snapshot_id="snapshot-2",
            not_after=datetime.fromtimestamp(WALL_EPOCH + 550, UTC).isoformat(),
        ),
    ).json()
    assert second["revision"] > first["revision"]
    clock.now += 50
    assert lookup(client).json()["snapshot_ref"] == "snapshot-2"
    assert publish(client).status_code == 410


def test_sandbox_names_isolate_records_and_allow_same_snapshot_id(service):
    """The unique sandbox name, rather than a custom namespace, selects records."""
    client, _ = service
    publish(client)
    publish(client, snapshot(name="hermes-unique-b", context={"different": True}))
    assert lookup(client, "hermes-unique-b", include_content=True).json()["snapshot"][
        "context"
    ] == {"different": True}
    assert lookup(client, "unknown").status_code == 404


def test_tool_writes_and_reads_do_not_extend_snapshot_lifetime(service):
    """A long-running tool cannot keep old context alive through updates."""
    client, clock = service
    receipt = publish(client).json()
    clock.now += 299
    response = client.post("/v1/tool-records", headers=PUBLISH_HEADERS, json=tool_record())
    assert response.json()["expires_at"] == receipt["expires_at"]
    assert lookup(client).status_code == 200
    clock.now += 1
    assert lookup(client).status_code == 404
    assert (
        client.post("/v1/tool-records", headers=PUBLISH_HEADERS, json=tool_record()).status_code
        == 404
    )


def test_tool_cannot_reference_a_different_sandbox_snapshot(service):
    """A matching snapshot ID under another name does not satisfy the reference."""
    client, _ = service
    publish(client, snapshot(name="hermes-unique-b"))
    response = client.post("/v1/tool-records", headers=PUBLISH_HEADERS, json=tool_record())
    assert response.status_code == 404


def test_tool_events_are_immutable_and_idempotent(service):
    """Pending/completed are different event IDs; no in-place history rewrite."""
    client, _ = service
    publish(client)
    first = client.post("/v1/tool-records", headers=PUBLISH_HEADERS, json=tool_record())
    retry = client.post("/v1/tool-records", headers=PUBLISH_HEADERS, json=tool_record())
    assert first.status_code == 201 and retry.status_code == 200
    assert (
        client.post(
            "/v1/tool-records", headers=PUBLISH_HEADERS, json=tool_record(state="completed")
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/v1/tool-records",
            headers=PUBLISH_HEADERS,
            json=tool_record("record-2", state="completed"),
        ).status_code
        == 201
    )
    assert lookup(client).json()["recent_tool_record_refs"] == ["record-2", "record-1"]


@pytest.mark.parametrize(
    ("raw", "status"),
    [
        (b'{"sandbox_name":"a","sandbox_name":"b"}', 400),
        (b'{"context":NaN}', 400),
        (b"\xff", 400),
        (b"{}", 422),
    ],
)
def test_invalid_json_is_rejected_without_echoing_body(service, raw, status):
    """Invalid input produces stable reasons without Pydantic's raw input echoes."""
    client, _ = service
    result = client.post(
        "/v1/context-snapshots",
        headers={**PUBLISH_HEADERS, "Content-Type": "application/json"},
        content=raw,
    )
    assert result.status_code == status
    assert "input" not in result.json()


def test_unknown_fields_are_not_silently_accepted(service):
    """A caller cannot supply server-assigned expiry or a custom namespace."""
    client, _ = service
    assert publish(client, snapshot(expires_at="2099-01-01T00:00:00Z")).status_code == 422
    assert publish(client, snapshot(namespace="not-supported")).status_code == 422


def test_authenticated_reader_can_request_content_without_server_opt_in():
    """Reader authentication and an explicit request suffice for a content read."""
    with TestClient(create_app(publisher_token=PUBLISHER, reader_token=READER)) as client:
        publish(client)
        assert lookup(client).status_code == 200
        response = lookup(client, include_content=True)
        assert response.status_code == 200
        assert response.json()["snapshot"]["context"] == snapshot()["context"]


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, PUBLISH_HEADERS])
def test_content_reads_require_reader_authentication(service, headers):
    """An explicit content request never bypasses reader-role authentication."""
    client, _ = service
    publish(client)
    response = client.post(
        "/v1/context-lookups",
        headers=headers,
        json={"sandbox_name": "hermes-unique-a", "include_content": True},
    )
    assert response.status_code == 401
    assert "Fictional workshop" not in response.text


def test_size_gate_measures_streamed_raw_bytes_before_json_parsing():
    """The service enforces actual byte length even without Content-Length."""
    with TestClient(
        create_app(publisher_token=PUBLISHER, reader_token=READER, max_body_bytes=256)
    ) as client:
        result = client.post(
            "/v1/context-snapshots",
            headers={**PUBLISH_HEADERS, "Content-Type": "application/json"},
            content=iter([b" " * 200, b" " * 57]),
        )
        assert result.status_code == 413


def test_media_types_and_compression_are_explicit(service):
    """Only uncompressed JSON is accepted; no implicit parser conversions."""
    client, _ = service
    assert (
        client.post(
            "/v1/context-snapshots",
            headers={**PUBLISH_HEADERS, "Content-Type": "text/plain"},
            content="{}",
        ).status_code
        == 415
    )
    assert (
        client.post(
            "/v1/context-snapshots",
            headers={**PUBLISH_HEADERS, "Content-Encoding": "gzip"},
            json=snapshot(),
        ).status_code
        == 415
    )


def test_record_capacity_fails_closed_without_evicting_live_records():
    """Store pressure must not silently discard still-required context."""
    store = ContextStore(max_records=1)
    with TestClient(
        create_app(publisher_token=PUBLISHER, reader_token=READER, store=store)
    ) as client:
        assert publish(client).status_code == 201
        assert publish(client, snapshot(snapshot_id="snapshot-2")).status_code == 507
        assert lookup(client).json()["snapshot_ref"] == "snapshot-1"


def test_store_byte_budget_is_separate_from_transport_limit():
    """Several small, valid requests may exhaust aggregate memory safely."""
    store = ContextStore(max_bytes=1)
    with TestClient(
        create_app(publisher_token=PUBLISHER, reader_token=READER, store=store)
    ) as client:
        assert publish(client).status_code == 507
        assert lookup(client).status_code == 404


def test_expired_payloads_are_purged_and_capacity_can_be_reused():
    """Expiry frees stored content; the fixed deadline rejects late exact retries."""
    clock = Clock()
    store = ContextStore(
        clock=clock, wall_clock=lambda: WALL_EPOCH + clock.now - 100, ttl_seconds=1, max_records=1
    )
    with TestClient(
        create_app(publisher_token=PUBLISHER, reader_token=READER, store=store)
    ) as client:
        publish(client, snapshot(not_after=datetime.fromtimestamp(WALL_EPOCH + 1, UTC).isoformat()))
        clock.now += 1
        assert (
            publish(
                client,
                snapshot(
                    snapshot_id="snapshot-2",
                    not_after=datetime.fromtimestamp(WALL_EPOCH + 2, UTC).isoformat(),
                ),
            ).status_code
            == 201
        )
        assert lookup(client).json()["snapshot_ref"] == "snapshot-2"


def test_restart_does_not_restore_context():
    """Two application instances have separate ephemeral stores."""
    with TestClient(create_app(publisher_token=PUBLISHER, reader_token=READER)) as client:
        publish(client)
    with TestClient(create_app(publisher_token=PUBLISHER, reader_token=READER)) as client:
        assert lookup(client).status_code == 404


@pytest.mark.parametrize("token", ["", "short", "has whitespace " * 4])
def test_startup_rejects_weak_or_malformed_credentials(token):
    """Configuration errors fail before serving; token values stay out of the error."""
    with pytest.raises(ValueError):
        create_app(publisher_token=token, reader_token=READER)


def test_publisher_and_reader_tokens_must_differ():
    """Role separation must not accidentally collapse during configuration."""
    with pytest.raises(ValueError):
        create_app(publisher_token=PUBLISHER, reader_token=PUBLISHER)


def test_expired_exact_publication_cannot_resurrect_after_store_restart():
    """The deadline is checked on every write, even when no prior ID is remembered."""
    clock = Clock()
    original = snapshot()
    for step in [0, 300]:
        clock.now = 100 + step
        store = ContextStore(clock=clock, wall_clock=lambda: WALL_EPOCH + clock.now - 100)
        with TestClient(
            create_app(publisher_token=PUBLISHER, reader_token=READER, store=store)
        ) as client:
            response = publish(client, original)
            assert response.status_code == (201 if step == 0 else 410)


def test_deadlines_beyond_server_ttl_are_rejected(service):
    """The caller may shorten retention but cannot exceed the configured ceiling."""
    client, _ = service
    too_late = datetime.fromtimestamp(WALL_EPOCH + 301, UTC).isoformat()
    assert publish(client, snapshot(not_after=too_late)).status_code == 422


def test_identical_older_retry_does_not_change_latest_snapshot(service):
    """Retrying an older live write must not supersede a genuinely newer snapshot."""
    client, _ = service
    publish(client)
    publish(client, snapshot(snapshot_id="snapshot-2"))
    assert publish(client).status_code == 200
    assert lookup(client).json()["snapshot_ref"] == "snapshot-2"


def test_concurrent_duplicate_writes_commit_once():
    """Concurrent callers share atomic deduplication, not a check-then-insert race."""
    store = ContextStore()
    record = SnapshotInput.model_validate_json(json.dumps(snapshot()))
    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(lambda _: store.publish(record), range(32)))
    assert sum(not r["duplicate"] for r in receipts) == 1
    assert {r["revision"] for r in receipts} == {1}


def test_content_response_explicitly_reports_truncated_tool_window(service):
    """Lookup bounds its response without pretending omitted tool records do not exist."""
    client, _ = service
    publish(client)
    for index in range(21):
        assert (
            client.post(
                "/v1/tool-records", headers=PUBLISH_HEADERS, json=tool_record(f"record-{index}")
            ).status_code
            == 201
        )
    data = lookup(client).json()
    assert len(data["recent_tool_record_refs"]) == 20
    assert data["tool_records_truncated"] is True
    assert len(data["tool_record_expires_at"]) == 20
    assert set(data["tool_record_expires_at"]) == set(data["recent_tool_record_refs"])
    assert data["recent_tool_record_refs"][0] == "record-20"


@pytest.mark.parametrize("ttl", [0, -1, float("nan"), float("inf")])
def test_invalid_ttl_is_rejected(ttl):
    """Retention must remain a finite, positive maximum."""
    with pytest.raises(ValueError):
        ContextStore(ttl_seconds=ttl)


@pytest.mark.parametrize("field", ["max_records", "max_bytes"])
def test_invalid_store_limits_are_rejected(field):
    """The in-memory store cannot start with disabled capacity gates."""
    with pytest.raises(ValueError):
        ContextStore(**{field: 0})


@pytest.mark.parametrize(
    "option",
    [{"max_body_bytes": 0}, {"read_timeout_seconds": 0}, {"read_timeout_seconds": float("nan")}],
)
def test_invalid_transport_settings_are_rejected(option):
    """Transport budgets are validated before serving any requests."""
    with pytest.raises(ValueError):
        create_app(publisher_token=PUBLISHER, reader_token=READER, **option)


def test_liveness_never_exposes_names_or_payloads(service):
    """Health checks are unauthenticated and deliberately have a fixed response."""
    client, _ = service
    publish(client)
    assert client.get("/health").json() == {"status": "ok"}


def test_store_purges_idle_payloads_without_lookup():
    """Maintenance releases payload bytes, not merely their lookup visibility."""
    clock = Clock()
    store = ContextStore(clock=clock, wall_clock=lambda: WALL_EPOCH + clock.now - 100)
    store.publish(SnapshotInput.model_validate_json(json.dumps(snapshot())))
    assert store._bytes > 0
    clock.now += 300
    store.purge()
    assert store._bytes == 0 and not store._entries


def test_cannot_trick_json_parser_with_utf16_or_surrogates(service):
    """Only UTF-8 JSON with Unicode scalar values may enter the store."""
    client, _ = service
    raw = json.dumps(snapshot()).encode("utf-16")
    response = client.post(
        "/v1/context-snapshots",
        headers={**PUBLISH_HEADERS, "Content-Type": "application/json"},
        content=raw,
    )
    assert response.status_code == 400
    result = client.post(
        "/v1/context-snapshots",
        headers={**PUBLISH_HEADERS, "Content-Type": "application/json"},
        content=json.dumps(snapshot(context={"bad": "\ud800"})),
    )
    assert result.status_code in (400, 422)


@pytest.mark.parametrize("overflow", ["1e999", "-1e999"])
@pytest.mark.parametrize("route", ["/v1/context-snapshots", "/v1/tool-records"])
def test_overflowing_json_numbers_do_not_cause_internal_errors(service, overflow, route):
    """Finite-number validation covers numeric overflow, not just NaN literals."""
    client, _ = service
    if route.endswith("tool-records"):
        publish(client)
        body = tool_record(arguments={"value": "OVERFLOW"})
    else:
        body = snapshot(context={"value": "OVERFLOW"})
    result = client.post(
        route,
        headers={**PUBLISH_HEADERS, "Content-Type": "application/json"},
        content=json.dumps(body).replace('"OVERFLOW"', overflow),
    )
    assert result.status_code == 400
    assert result.json() == {"detail": "invalid_json"}


def test_expired_latest_snapshot_never_falls_back_to_older_context(service):
    """A newer short-lived publication must not resurrect an earlier conversation view."""
    client, clock = service
    publish(client)
    publish(
        client,
        snapshot(
            snapshot_id="snapshot-2",
            not_after=datetime.fromtimestamp(WALL_EPOCH + 100, UTC).isoformat(),
        ),
    )
    clock.now += 100
    assert lookup(client).status_code == 404
    assert publish(client).status_code == 200  # This is a retry, not a fresh model input.
    assert lookup(client).status_code == 404


def test_content_and_error_responses_are_not_cacheable(service):
    """Intermediary caches must not extend the lifetime of conversation data."""
    client, _ = service
    publish(client)
    assert lookup(client, include_content=True).headers["cache-control"] == "no-store"
    assert client.post("/v1/context-lookups", json={}).headers["cache-control"] == "no-store"


def test_nested_duplicate_keys_are_rejected(service):
    """Duplicate detection applies inside context, not only to routing fields."""
    client, _ = service
    raw = json.dumps(snapshot()).replace('"context": {', '"context": {"x":1,"x":2,')
    result = client.post(
        "/v1/context-snapshots",
        headers={**PUBLISH_HEADERS, "Content-Type": "application/json"},
        content=raw,
    )
    assert result.status_code == 400 and lookup(client).status_code == 404


def test_duplicate_authorization_headers_are_rejected(service):
    """Multiple credentials must not be interpreted differently by intermediaries."""
    client, _ = service
    response = client.post(
        "/v1/context-snapshots",
        headers=[
            ("Authorization", f"Bearer {PUBLISHER}"),
            ("Authorization", f"Bearer {PUBLISHER}"),
        ],
        json=snapshot(),
    )
    assert response.status_code == 401


@pytest.mark.parametrize("disconnect", [False, True])
@pytest.mark.parametrize("read_timeout", [None, 0.01])
def test_incomplete_body_has_a_bounded_sanitized_failure(disconnect, read_timeout):
    """Stalls and disconnects never retain partial context or leak request bytes."""
    import asyncio

    options = {} if read_timeout is None else {"read_timeout_seconds": read_timeout}
    app = create_app(publisher_token=PUBLISHER, reader_token=READER, **options)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/context-snapshots",
        "raw_path": b"/v1/context-snapshots",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"authorization", f"Bearer {PUBLISHER}".encode()),
            (b"content-type", b"application/json"),
        ],
        "server": ("testserver", 80),
        "client": ("testclient", 1234),
    }
    sent = []

    async def exercise():
        first = True

        async def receive():
            nonlocal first
            if first:
                first = False
                return {
                    "type": "http.request",
                    "body": b'{"private-incomplete":',
                    "more_body": True,
                }
            if disconnect:
                return {"type": "http.disconnect"}
            await asyncio.Event().wait()

        async def send(message):
            sent.append(message)

        # The default one-second deadline must abort stalled bodies before this guard.
        await asyncio.wait_for(app(scope, receive, send), timeout=2)

    asyncio.run(exercise())
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == (400 if disconnect else 408)
    assert b"private-incomplete" not in b"".join(m.get("body", b"") for m in sent)
    with TestClient(app) as client:
        assert lookup(client).status_code == 404


@pytest.mark.parametrize("advance_monotonic", [False, True])
def test_either_clock_expiry_prevents_reading_context(advance_monotonic):
    """Wall jumps forward and monotonic expiry independently close the read gate."""
    mono, wall = Clock(), Clock()
    store = ContextStore(clock=mono, wall_clock=lambda: WALL_EPOCH + wall.now - 100)
    store.publish(SnapshotInput.model_validate_json(json.dumps(snapshot())))
    (mono if advance_monotonic else wall).now += 300
    from nomos_context_service.store import StoreError

    with pytest.raises(StoreError, match="context_unavailable"):
        store.lookup("hermes-unique-a")


@pytest.mark.parametrize("call_id", ["functions.terminal:0", "x" * 200])
def test_provider_tool_call_ids_are_opaque_strings(service, call_id):
    """Do not impose locally generated ID syntax on a provider-issued correlation ID."""
    client, _ = service
    publish(client)
    assert (
        client.post(
            "/v1/tool-records", headers=PUBLISH_HEADERS, json=tool_record(tool_call_id=call_id)
        ).status_code
        == 201
    )


@pytest.mark.parametrize("call_id", ["", "x" * 257])
def test_provider_tool_call_ids_remain_bounded(service, call_id):
    """Opaque is not unlimited: match the existing Nomos plugin's 1–256 bound."""
    client, _ = service
    publish(client)
    assert (
        client.post(
            "/v1/tool-records", headers=PUBLISH_HEADERS, json=tool_record(tool_call_id=call_id)
        ).status_code
        == 422
    )


def test_observed_request_ids_do_not_need_generated_id_syntax(service):
    """Supervisor-supplied audit fields are bounded opaque values, not store keys."""
    client, _ = service
    publish(client)
    assert (
        lookup(
            client, observed_sandbox_id="external:sandbox/1", http_request_id="request:2"
        ).status_code
        == 200
    )


def test_idle_cleanup_runs_every_minute_and_stops_on_shutdown(monkeypatch):
    """Drive the actual lifespan task without sleeping or starting a server thread."""
    import asyncio

    from nomos_context_service import server

    store = ContextStore()
    app = create_app(publisher_token=PUBLISHER, reader_token=READER, store=store)

    async def exercise():
        """Advance two maintenance intervals, then verify shutdown awaits cancellation."""
        wakeups = asyncio.Queue()
        releases = asyncio.Queue()
        purges = asyncio.Queue()
        original_purge = store.purge
        maintenance_tasks = []

        async def controlled_sleep(delay):
            """Expose each timer wait while keeping cancellation behavior unchanged."""
            task = asyncio.current_task()
            if not maintenance_tasks:
                maintenance_tasks.append(task)
            assert task is maintenance_tasks[0]
            wakeups.put_nowait(delay)
            await releases.get()

        def observe_cleanup():
            """Count completed real purges rather than merely observing timer creation."""
            original_purge()
            purges.put_nowait(True)

        monkeypatch.setattr(server.asyncio, "sleep", controlled_sleep)
        monkeypatch.setattr(store, "purge", observe_cleanup)
        async with app.router.lifespan_context(app):
            for _ in range(2):
                assert await asyncio.wait_for(wakeups.get(), timeout=1) == 60
                releases.put_nowait(True)
                assert await asyncio.wait_for(purges.get(), timeout=1)
            # Block on the next timer before closing, proving repeat scheduling.
            assert await asyncio.wait_for(wakeups.get(), timeout=1) == 60
            assert not maintenance_tasks[0].done()
        assert maintenance_tasks[0].done() and maintenance_tasks[0].cancelled()
        assert purges.empty()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("state", "review"),
    [
        (
            "execution_pending",
            {"status": "approved", "decision_id": "decision/42", "council_id": "council-A"},
        ),
        ("completed", {"status": "approved", "decision_id": "decision/42"}),
        ("failed", {"status": "approved", "decision_id": "decision/42"}),
        ("denied", {"status": "approved", "decision_id": "decision/42"}),
        (
            "denied",
            {
                "status": "denied",
                "decision_id": "decision/43",
                "guidance": ["Remove credentials before uploading."],
            },
        ),
        ("denied", {"status": "error", "decision_id": None}),
    ],
)
def test_council_review_is_retained_with_independent_execution_state(service, state, review):
    """Council's reported outcome and reference do not replace the tool lifecycle."""
    client, _ = service
    publish(client)
    body = tool_record(state=state, council_review=review)
    assert client.post("/v1/tool-records", headers=PUBLISH_HEADERS, json=body).status_code == 201
    data = lookup(client, include_content=True).json()["tool_records"][0]
    assert data["state"] == state
    for key, value in review.items():
        assert data["council_review"][key] == value
    assert data["council_review"]["guidance_truncated"] is False
    metadata = lookup(client)
    assert "Remove credentials" not in metadata.text
    assert "council_review" not in metadata.text
    if review.get("decision_id"):
        assert review["decision_id"] not in metadata.text


@pytest.mark.parametrize(
    "review",
    [
        {},
        {"status": "allow"},
        {"status": "approved", "decision_id": ""},
        {"status": "approved", "decision_id": "x" * 257},
        {"status": "approved", "decision_id": 42},
        {"status": "approved", "council_id": ""},
        {"status": "denied", "guidance": "Remove credentials."},
        {"status": "denied", "guidance": [42]},
        {"status": "denied", "guidance": [""]},
        {"status": "denied", "guidance": ["x" * 1025]},
        {"status": "denied", "guidance": ["Advice"] * 6},
        {"status": "denied", "guidance_truncated": "true"},
        {"status": "approved", "writ": "not-for-context-storage"},
    ],
)
def test_invalid_council_review_is_rejected_without_echoing_input(service, review):
    """Strict bounded review fields reject oversized guidance and accidental credentials."""
    client, _ = service
    publish(client)
    response = client.post(
        "/v1/tool-records", headers=PUBLISH_HEADERS, json=tool_record(council_review=review)
    )
    assert response.status_code == 422
    assert "not-for-context-storage" not in response.text
    assert lookup(client).json()["recent_tool_record_refs"] == []


def test_review_guidance_boundary_and_immutable_decision_reference(service):
    """Guidance is preserved as supplied; changing a live review needs a new record."""
    client, _ = service
    publish(client)
    review = {"status": "denied", "guidance": ["界" * 1024] * 5, "guidance_truncated": True}
    body = tool_record(state="denied", council_review=review)
    assert client.post("/v1/tool-records", headers=PUBLISH_HEADERS, json=body).status_code == 201
    assert client.post("/v1/tool-records", headers=PUBLISH_HEADERS, json=body).status_code == 200
    saved = lookup(client, include_content=True).json()["tool_records"][0]["council_review"]
    assert saved["guidance"] == review["guidance"]
    assert saved["guidance_truncated"] is True
    body["council_review"]["decision_id"] = "different-review"
    assert client.post("/v1/tool-records", headers=PUBLISH_HEADERS, json=body).status_code == 409


@pytest.mark.parametrize("incomplete", [True, False, None])
def test_argument_completeness_is_reported_in_content_only(service, incomplete):
    """Publishers report complete, partial or legacy-unknown business arguments."""
    client, _ = service
    publish(client)
    body = tool_record(arguments_incomplete=incomplete)
    assert client.post("/v1/tool-records", headers=PUBLISH_HEADERS, json=body).status_code == 201
    content = lookup(client, include_content=True).json()
    assert content["tool_records"][0]["arguments_incomplete"] is incomplete
    assert "arguments_incomplete" not in lookup(client).text


def test_missing_argument_completeness_means_unknown_not_complete(service):
    """Legacy records must never falsely attest that no business data was omitted."""
    client, _ = service
    publish(client)
    assert (
        client.post("/v1/tool-records", headers=PUBLISH_HEADERS, json=tool_record()).status_code
        == 201
    )
    assert (
        lookup(client, include_content=True).json()["tool_records"][0]["arguments_incomplete"]
        is None
    )


@pytest.mark.parametrize("incomplete", [0, 1, "true", "false", [], {}])
def test_non_boolean_argument_completeness_is_rejected(service, incomplete):
    """Only booleans or null are accepted without coercing a publisher's claim."""
    client, _ = service
    publish(client)
    response = client.post(
        "/v1/tool-records",
        headers=PUBLISH_HEADERS,
        json=tool_record(arguments_incomplete=incomplete),
    )
    assert response.status_code == 422
    assert lookup(client).json()["recent_tool_record_refs"] == []


def test_tool_expiry_map_reports_each_original_parent_deadline(service):
    """A lookup's newest snapshot deadline must not extend older tool records."""
    client, clock = service
    older = datetime.fromtimestamp(WALL_EPOCH + 50, UTC).isoformat()
    newer = datetime.fromtimestamp(WALL_EPOCH + 200, UTC).isoformat()
    assert publish(client, snapshot(not_after=older)).status_code == 201
    assert (
        client.post("/v1/tool-records", headers=PUBLISH_HEADERS, json=tool_record()).status_code
        == 201
    )
    assert publish(client, snapshot(snapshot_id="snapshot-2", not_after=newer)).status_code == 201
    later_event = tool_record("record-2", snapshot_id="snapshot-2")
    assert (
        client.post("/v1/tool-records", headers=PUBLISH_HEADERS, json=later_event).status_code
        == 201
    )
    expected = {"record-2": newer, "record-1": older}
    for content in (False, True):
        data = lookup(client, include_content=content).json()
        assert data["tool_record_expires_at"] == expected
        assert set(data["tool_record_expires_at"]) == set(data["recent_tool_record_refs"])
    # A retry retains the original parent's deadline, despite later receipt time.
    clock.now += 10
    assert (
        client.post("/v1/tool-records", headers=PUBLISH_HEADERS, json=tool_record()).status_code
        == 200
    )
    assert lookup(client).json()["tool_record_expires_at"] == expected
    clock.now += 41
    remaining = lookup(client, include_content=True).json()
    assert remaining["tool_record_expires_at"] == {"record-2": newer}
    assert [record["record_id"] for record in remaining["tool_records"]] == ["record-2"]


def test_tool_expiry_map_does_not_revive_older_snapshot_when_newest_expires(service):
    """The latest-pointer fence applies even when an older tool would still be live."""
    client, clock = service
    publish(client)
    client.post("/v1/tool-records", headers=PUBLISH_HEADERS, json=tool_record())
    short = datetime.fromtimestamp(WALL_EPOCH + 5, UTC).isoformat()
    publish(client, snapshot(snapshot_id="snapshot-2", not_after=short))
    clock.now += 6
    assert lookup(client).status_code == 404
    assert lookup(client, include_content=True).status_code == 404


def test_tool_expiry_metadata_cannot_be_supplied_by_publisher(service):
    """Only the service derives record expiry from its accepted immutable parent."""
    client, _ = service
    publish(client)
    supplied = tool_record(tool_record_expires_at={"record-1": "2099-01-01T00:00:00Z"})
    assert (
        client.post("/v1/tool-records", headers=PUBLISH_HEADERS, json=supplied).status_code == 422
    )
