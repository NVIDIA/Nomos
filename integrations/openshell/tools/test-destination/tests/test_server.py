# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Check the independent capture evidence and its limits, not model quality."""

import asyncio
import base64
import hashlib

import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from test_destination import server
from test_destination.server import create_app


def capture_scope():
    """Use the ASGI receive interface to exercise incomplete request streams."""
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/publish",
        "raw_path": b"/publish",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json")],
        "server": ("testserver", 80),
        "client": ("testclient", 1234),
    }


def test_body_read_deadline_counts_arrival_without_retaining_partial_bytes():
    """A stalled request returns 408 and remains visible in arrival counts."""
    app = create_app(read_timeout_seconds=0.02)
    sent = []

    async def exercise():
        first = True

        async def receive():
            nonlocal first
            if first:
                first = False
                return {
                    "type": "http.request",
                    "body": b'{"partial":',
                    "more_body": True,
                }
            await asyncio.Event().wait()

        async def send(message):
            sent.append(message)

        await app(capture_scope(), receive, send)

    asyncio.run(exercise())
    assert next(m for m in sent if m["type"] == "http.response.start")["status"] == 408
    with TestClient(app) as client:
        result = client.get("/requests").json()
    assert result == {"received_count": 1, "recorded_count": 0, "requests": []}


def test_disconnected_body_counts_arrival_without_retaining_partial_bytes():
    """A partial-body disconnect leaves an arrival but no complete record."""
    app = create_app()

    async def exercise():
        events = iter(
            [
                {"type": "http.request", "body": b'{"partial":', "more_body": True},
                {"type": "http.disconnect"},
            ]
        )

        async def receive():
            return next(events)

        async def send(_message):
            pass

        await app(capture_scope(), receive, send)

    with pytest.raises(ClientDisconnect):
        asyncio.run(exercise())
    with TestClient(app) as client:
        result = client.get("/requests").json()
    assert result == {"received_count": 1, "recorded_count": 0, "requests": []}


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_body_read_deadline_requires_a_finite_positive_budget(timeout):
    """The injectable test budget must not accidentally disable the deadline."""
    with pytest.raises(ValueError, match="finite and positive"):
        create_app(read_timeout_seconds=timeout)


def test_cli_can_increase_capture_capacity(monkeypatch):
    """The CLI must not silently keep the 100-arrival limit for larger runs."""
    monkeypatch.setattr("sys.argv", ["test-destination", "--max-records", "2048"])

    def check_app(app, **kwargs):
        with TestClient(app) as client:
            for _ in range(101):
                assert client.post("/publish", content=b"{}").status_code == 200
            assert client.get("/requests").json()["recorded_count"] == 101

    monkeypatch.setattr(server.uvicorn, "run", check_app)
    server.main()


def test_preserves_original_json_bytes_and_counts_each_arrival():
    """Whitespace and non-ASCII encoding must survive capture unchanged."""
    with TestClient(create_app()) as client:
        assert client.get("/requests").json()["received_count"] == 0
        body = '{ "note": "café", "n": 1 }\n'.encode()
        response = client.post(
            "/publish", content=body, headers={"content-type": "application/json"}
        )
        assert response.status_code == 200
        records = client.get("/requests").json()
        assert records["received_count"] == records["recorded_count"] == 1
        captured = records["requests"][0]
        assert base64.b64decode(captured["body_base64"]) == body
        assert captured["sha256"] == hashlib.sha256(body).hexdigest()
        assert captured["body_bytes"] == len(body)
        client.post("/publish", content=b"second")
        assert client.get("/requests").json()["received_count"] == 2


def test_rejected_oversized_body_is_still_counted_as_an_arrival():
    """A destination rejection must never look like an OpenShell-blocked call."""
    with TestClient(create_app(max_body_bytes=2)) as client:
        response = client.post("/publish", content=iter([b"ab", b"c"]))
        assert response.status_code == 413
        records = client.get("/requests").json()
        assert records["received_count"] == 1
        assert records["recorded_count"] == 0


def test_capacity_does_not_evict_or_hide_arrivals():
    """Full capture memory rejects new records while preserving the arrival count."""
    with TestClient(create_app(max_records=1)) as client:
        assert client.post("/publish", content=b"first").status_code == 200
        assert client.post("/publish", content=b"second").status_code == 507
        records = client.get("/requests").json()
        assert records["received_count"] == 2
        assert records["recorded_count"] == 1
        assert base64.b64decode(records["requests"][0]["body_base64"]) == b"first"


def test_readiness_and_observation_do_not_count_as_publications():
    """Inspection requests cannot be mistaken for sandbox traffic."""
    with TestClient(create_app()) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/requests").json()["received_count"] == 0
        assert client.get("/unknown").status_code == 404
        assert client.delete("/requests").status_code == 405


def test_restart_uses_a_new_store():
    """Records belong to one application process, not durable storage."""
    with TestClient(create_app()) as first:
        first.post("/publish", content=b"one")
    with TestClient(create_app()) as second:
        assert second.get("/requests").json()["received_count"] == 0


def test_other_api_routes_share_exact_byte_capture_and_target_evidence():
    """Prove different verbs/routes are observable without a publication schema."""
    body = b'{ "message": "Fictional content", "revision": 2 }\n'
    with TestClient(create_app()) as client:
        for method, path in [("POST", "/messages"), ("PUT", "/documents/123")]:
            assert client.request(method, path, content=body).status_code == 200
        records = client.get("/requests").json()
        assert records["received_count"] == 2
        assert [(r["method"], r["path"]) for r in records["requests"]] == [
            ("POST", "/messages"),
            ("PUT", "/documents/123"),
        ]
        for record in records["requests"]:
            assert base64.b64decode(record["body_base64"]) == body
            assert record["sha256"] == hashlib.sha256(body).hexdigest()
        assert client.put("/messages", content=body).status_code == 405
        assert client.get("/requests").json()["received_count"] == 2


def test_issue_submission_records_actual_target_and_exact_body():
    """Issue-shaped requests reach the same independent evidence store."""
    path = "/repos/example/project/issues"
    body = b'{ "title": "Integration error", "body": "Details\\nattached." }\n'
    with TestClient(create_app()) as client:
        assert client.post(path, content=body).status_code == 200
        record = client.get("/requests").json()["requests"][0]
        assert (record["method"], record["path"]) == ("POST", path)
        assert base64.b64decode(record["body_base64"]) == body
        assert record["sha256"] == hashlib.sha256(body).hexdigest()
        assert client.put(path, content=body).status_code == 405
        assert client.get("/requests").json()["received_count"] == 1


def test_publication_capture_accepts_all_methods_including_extension_tokens():
    """Wildcard policy must reach capture, including JSON bodies on GET/HEAD."""
    body = b'{ "publication": "Fictional content" }'
    methods = [
        "GET",
        "HEAD",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "OPTIONS",
        "TRACE",
        "PURGE",
    ]
    with TestClient(create_app()) as client:
        for method in methods:
            response = client.request(method, "/publish", content=body)
            assert response.status_code == 200
            if method == "HEAD":
                assert response.content == b""
        records = client.get("/requests").json()
        assert records["received_count"] == len(methods)
        assert [record["method"] for record in records["requests"]] == methods
        for record in records["requests"]:
            assert record["path"] == "/publish"
            assert base64.b64decode(record["body_base64"]) == body
            assert record["sha256"] == hashlib.sha256(body).hexdigest()
        assert client.delete("/requests").status_code == 405
        assert client.get("/requests").json()["received_count"] == len(methods)


def test_incremental_inspection_keeps_global_failure_counts_and_generic_routes():
    """Cursor export avoids redownloading bodies without concealing failures."""
    with TestClient(create_app(max_records=2)) as client:
        assert client.post("/submit", content=b"one").status_code == 200
        assert client.post("/support", content=b"two").status_code == 200
        result = client.get("/requests?after=1").json()
        assert result["received_count"] == result["recorded_count"] == 2
        assert [r["request_id"] for r in result["requests"]] == [2]
        assert base64.b64decode(result["requests"][0]["body_base64"]) == b"two"
        assert client.post("/submit", content=b"three").status_code == 507
        result = client.get("/requests?after=2").json()
        assert result["received_count"] == 3
        assert result["recorded_count"] == 2
        assert result["requests"] == []
        assert client.get("/requests?after=-1").status_code == 422
        assert client.get("/submit").status_code == 405
