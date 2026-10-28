# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Record bounded HTTP request bodies for isolated OpenShell integration tests.

This is the destination that sandbox requests try to reach, not a simulated
model. Records live only in memory. Run one worker so every inspection sees the
same store. Use fictional data: inspection routes have no authentication.
"""

import argparse
import asyncio
import base64
import hashlib
from dataclasses import dataclass, field
from threading import Lock

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

MAX_BODY_BYTES = 256 * 1024
MAX_RECORDS = 100


@dataclass
class CaptureStore:
    """Keep exact bodies and a separate count of every entered capture handler."""

    received_count: int = 0
    records: list[dict] = field(default_factory=list)
    lock: Lock = field(default_factory=Lock)

    def begin(self) -> int:
        """Count arrival even if reading or recording the body later fails."""
        with self.lock:
            self.received_count += 1
            return self.received_count

    def record(
        self, request_id: int, body: bytes, capacity: int, method: str, path: str
    ) -> None:
        """Retain original bytes without parsing/reformatting or silent eviction."""
        record = {
            "request_id": request_id,
            "method": method,
            "path": path,
            "body_bytes": len(body),
            "body_base64": base64.b64encode(body).decode("ascii"),
            "sha256": hashlib.sha256(body).hexdigest(),
        }
        with self.lock:
            if len(self.records) >= capacity:
                raise HTTPException(507, "record_capacity_reached")
            self.records.append(record)

    def snapshot(self, after: int = 0) -> dict:
        """Return a consistent count and copy of the retained records."""
        with self.lock:
            return {
                "received_count": self.received_count,
                "recorded_count": len(self.records),
                "requests": [
                    record for record in self.records if record["request_id"] > after
                ],
            }


def create_app(
    *, max_body_bytes: int = MAX_BODY_BYTES, max_records: int = MAX_RECORDS
) -> FastAPI:
    """Create an isolated capture store; smaller limits support boundary tests."""
    if max_body_bytes < 1 or max_records < 1:
        raise ValueError("capture limits must be positive")
    store = CaptureStore()
    app = FastAPI(title="Nomos test destination", docs_url=None, redoc_url=None)

    @app.get("/health")
    async def health() -> dict:
        """Report HTTP readiness without exposing recorded bodies."""
        return {"status": "ok"}

    @app.post("/repos/{owner}/{repo}/issues")
    @app.post("/submit")
    @app.post("/support")
    @app.post("/messages")
    @app.put("/documents/{document_id}")
    async def capture(request: Request) -> dict:
        """Record bytes exactly as received; middleware owns content decisions."""
        request_id = store.begin()
        body = bytearray()
        try:
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    if len(body) + len(chunk) > max_body_bytes:
                        raise HTTPException(413, "body_over_limit")
                    body.extend(chunk)
        except TimeoutError as error:
            raise HTTPException(408, "body_read_timeout") from error
        store.record(
            request_id, bytes(body), max_records, request.method, request.url.path
        )
        return {"received": True, "request_id": request_id, "body_bytes": len(body)}

    class CaptureAllMethods:
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            result = await capture(Request(scope, receive))
            await JSONResponse(result)(scope, receive, send)

    # An ASGI endpoint with methods=None accepts every method token; a function
    # endpoint would default to GET. Other routes keep their explicit methods.
    app.router.routes.append(Route("/publish", CaptureAllMethods()))

    @app.get("/requests")
    async def requests(after: int = Query(default=0, ge=0)) -> dict:
        """Return global counts and only records newer than an arrival cursor."""
        return store.snapshot(after)

    # No reset route: restarting is the explicit boundary between experiments.
    # This avoids an accidentally permitted client erasing enforcement evidence.
    return app


def main() -> None:
    """Run one foreground lab server; stop with Ctrl+C to discard records."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18081)
    parser.add_argument(
        "--max-records",
        type=int,
        default=MAX_RECORDS,
        help=(
            "Maximum retained arrivals; exporting does not free slots (default: 100)"
        ),
    )
    args = parser.parse_args()
    uvicorn.run(
        create_app(max_records=args.max_records),
        host=args.host,
        port=args.port,
        workers=1,
        access_log=False,
        proxy_headers=False,
        ws="none",
        limit_concurrency=32,
    )
