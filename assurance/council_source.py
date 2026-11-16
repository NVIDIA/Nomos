# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Authenticated, read-only bridge to Nomos's internal stateless MCP council.

NAT exposes tool discovery under /debug, but tool calls use /mcp. The bridge
terminates its own reader credential; it needs neither an agent token nor
database access. Ledger signature and chain verification remain in the observer.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, ValidationError

READ_TIMEOUT_SECONDS = 15
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_REQUEST_BYTES = 4096
PROTOCOL_VERSION = "2025-03-26"


class PageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    council_id: str = Field(default="", max_length=200)
    cursor: int | None = Field(default=None, ge=0)
    limit: int = Field(default=100, ge=1, le=200)


class EntryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    council_id: str = Field(default="", max_length=200)
    sequence: int = Field(ge=0)


def _rpc_result(data: bytes, content_type: str, request_id: int) -> dict:
    """Read one bounded JSON-RPC response, including the council's SSE framing."""
    if content_type.split(";", 1)[0] == "text/event-stream":
        messages = []
        event_data = []
        for line in [*data.decode("utf-8").splitlines(), ""]:
            if line.startswith("data:"):
                event_data.append(line[5:].removeprefix(" "))
            elif not line and event_data:
                messages.append(json.loads("\n".join(event_data)))
                event_data = []
    else:
        messages = [json.loads(data)]
    for message in messages:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise ValueError("invalid MCP response")
        if type(message.get("id")) is int and message["id"] == request_id:
            if "error" in message or not isinstance(message.get("result"), dict):
                raise ValueError("unsuccessful MCP response")
            return message["result"]
    raise ValueError("missing MCP response")


def create_app(
    *,
    upstream_url: str | None = None,
    council_id: str | None = None,
    token: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> FastAPI:
    upstream_url = os.environ["COUNCIL_SOURCE_URL"] if upstream_url is None else upstream_url
    council_id = os.environ["COUNCIL_ID"] if council_id is None else council_id
    token = os.environ["SOURCE_TOKEN"] if token is None else token
    parsed = urlsplit(upstream_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("COUNCIL_SOURCE_URL must be an HTTP(S) origin without credentials")
    if not token or not council_id or len(council_id) > 200:
        raise ValueError("SOURCE_TOKEN and COUNCIL_ID are required")
    endpoint = upstream_url.rstrip("/") + "/mcp"

    @asynccontextmanager
    async def lifespan(app):
        if client is not None:
            yield
        else:
            async with httpx.AsyncClient(trust_env=False) as owned_client:
                app.state.client = owned_client
                yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.client = client
    auth = HTTPBearer(auto_error=False)

    def authenticate(credentials: HTTPAuthorizationCredentials | None = Depends(auth)):
        if credentials is None or not hmac.compare_digest(
            credentials.credentials.encode(), token.encode()
        ):
            raise HTTPException(
                401, "Authentication required", headers={"WWW-Authenticate": "Bearer"}
            )

    @app.middleware("http")
    async def prevent_caching(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    async def arguments(request: Request, model: type[PageRequest] | type[EntryRequest]) -> dict:
        data = bytearray()
        try:
            async with asyncio.timeout(READ_TIMEOUT_SECONDS):
                async for chunk in request.stream():
                    data.extend(chunk)
                    if len(data) > MAX_REQUEST_BYTES:
                        raise HTTPException(413, "Ledger request exceeds 4 KiB limit")
            value = model.model_validate_json(data)
        except TimeoutError as exc:
            raise HTTPException(408, "Ledger request timed out") from exc
        except ValidationError as exc:
            raise HTTPException(422, "Invalid ledger request") from exc
        if "council_id" in value.model_fields_set and value.council_id != council_id:
            raise HTTPException(404, "Unknown council")
        return {**value.model_dump(), "council_id": council_id}

    async def rpc(body: dict) -> dict:
        async with app.state.client.stream(
            "POST",
            endpoint,
            json={"jsonrpc": "2.0", **body},
            headers={
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": PROTOCOL_VERSION,
            },
            auth=None,
            follow_redirects=False,
            timeout=READ_TIMEOUT_SECONDS,
        ) as response:
            response.raise_for_status()
            # Nomos's StatelessMCPWorker is required for requests to reach any replica.
            if response.headers.get("mcp-session-id"):
                raise ValueError("stateful MCP council is unsupported")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise ValueError("council response exceeds 8 MiB limit")
            if "id" not in body:
                return {}
            return _rpc_result(data, response.headers.get("content-type", ""), body["id"])

    async def read(tool: str, fields: dict) -> dict:
        try:
            async with asyncio.timeout(READ_TIMEOUT_SECONDS):
                initialized = await rpc(
                    {
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": PROTOCOL_VERSION,
                            "capabilities": {},
                            "clientInfo": {"name": "nomos-assurance-reader", "version": "1"},
                        },
                    }
                )
                if initialized.get("protocolVersion") != PROTOCOL_VERSION:
                    raise ValueError("unsupported MCP protocol")
                await rpc({"method": "notifications/initialized"})
                result = await rpc(
                    {
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": tool, "arguments": fields},
                    }
                )
            content = result.get("content")
            if result.get("isError") or not isinstance(content, list) or len(content) != 1:
                raise ValueError("unsuccessful ledger tool response")
            if not isinstance(content[0], dict) or content[0].get("type") != "text":
                raise ValueError("missing ledger tool text")
            payload = json.loads(content[0]["text"])
            if (
                not isinstance(payload, dict)
                or payload.get("error")
                or payload.get("council_id") != council_id
            ):
                raise ValueError("invalid ledger tool result")
            return payload
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError) as exc:
            # Errors may contain private upstream payloads, URLs, or credentials.
            raise HTTPException(502, "Council ledger read failed") from exc

    @app.get("/healthz")
    def health():
        return {"status": "up", "synthetic": False}

    @app.post("/debug/tools/call/council_ledger_page", dependencies=[Depends(authenticate)])
    async def page(request: Request):
        return await read("council_ledger_page", await arguments(request, PageRequest))

    @app.post("/debug/tools/call/council_ledger_get", dependencies=[Depends(authenticate)])
    async def get(request: Request):
        return await read("council_ledger_get", await arguments(request, EntryRequest))

    return app
