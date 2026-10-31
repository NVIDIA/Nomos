# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Serve optional short-lived context without coupling clients to a database.

Run one worker only. Clients must already have separately configured publisher
and reader bearer credentials. This API does not verify an agent's history,
identify the exact tool behind HTTP traffic, or authorize sandbox egress.
"""

import argparse
import asyncio
import hashlib
import hmac
import ipaddress
import json
import math
import os
import ssl
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TypeVar

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.datastructures import MutableHeaders
from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .models import ApiModel, LookupInput, SnapshotInput, ToolRecordInput
from .store import ContextStore, StoreError

Model = TypeVar("Model", bound=ApiModel)


def token_digest(token: str) -> bytes:
    """Validate an opaque configured token and retain a fixed-length hash only."""
    if (
        len(token) < 32
        or len(token) > 4096
        or not token.isascii()
        or any(c.isspace() for c in token)
    ):
        raise ValueError("credentials must be 32–4096 ASCII characters without whitespace")
    return hashlib.sha256(token.encode("ascii")).digest()


def _object_pairs(pairs: list[tuple[str, object]]) -> dict:
    """Reject duplicate keys at every JSON object level before schema validation."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    """Do not accept Python JSON's nonstandard NaN or infinity extension."""
    raise ValueError("nonfinite_json_number")


def _finite_float(value: str) -> float:
    """Reject valid JSON exponent syntax that overflows Python's finite float range."""
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("nonfinite_json_number")
    return result


class NoStoreMiddleware:
    """Prevent intermediaries from extending retention by caching API responses."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def no_store_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, no_store_send)


def create_app(
    *,
    publisher_token: str,
    reader_token: str,
    store: ContextStore | None = None,
    max_body_bytes: int = 16 * 1024,
    read_timeout_seconds: float = 1,
) -> FastAPI:
    """Build an isolated authenticated API; content access is explicitly opt-in."""
    publisher_hash, reader_hash = token_digest(publisher_token), token_digest(reader_token)
    if hmac.compare_digest(publisher_hash, reader_hash):
        raise ValueError("publisher and reader credentials must differ")
    if max_body_bytes < 1:
        raise ValueError("body limit must be positive")
    if not math.isfinite(read_timeout_seconds) or read_timeout_seconds <= 0:
        raise ValueError("read timeout must be finite and positive")
    context_store = store if store is not None else ContextStore()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        """Run idle cleanup on the serving event loop, without a separate thread."""

        async def cleanup() -> None:
            while True:
                await asyncio.sleep(60)
                context_store.purge()

        task = asyncio.create_task(cleanup())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(
        title="Nomos conversation context",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    app.add_middleware(NoStoreMiddleware)

    async def body(request: Request, expected_hash: bytes, model: type[Model]) -> Model:
        """Authenticate first, then bound actual bytes and sanitize parser errors."""
        authorization = request.headers.get("authorization", "")
        scheme, separator, token = authorization.partition(" ")
        candidate = hashlib.sha256(token.encode("utf-8")).digest()
        if (
            request.headers.getlist("authorization") != [authorization]
            or scheme.lower() != "bearer"
            or not separator
            or not hmac.compare_digest(candidate, expected_hash)
        ):
            raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            raise HTTPException(415, "application_json_required")
        if request.headers.get("content-encoding", "identity").lower() != "identity":
            raise HTTPException(415, "content_encoding_unsupported")
        raw = bytearray()
        try:
            async with asyncio.timeout(read_timeout_seconds):
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > max_body_bytes:
                        raise HTTPException(413, "body_over_limit")
                    raw.extend(chunk)
        except TimeoutError as error:
            raise HTTPException(408, "body_read_timeout") from error
        except ClientDisconnect as error:
            raise HTTPException(400, "incomplete_body") from error
        try:
            # Explicit UTF-8 avoids json.loads accepting UTF-16/32 byte payloads.
            text = raw.decode("utf-8")
            json.loads(
                text,
                object_pairs_hook=_object_pairs,
                parse_constant=_reject_constant,
                parse_float=_finite_float,
            )
        except (ValueError, RecursionError) as error:
            raise HTTPException(400, "invalid_json") from error
        try:
            return model.model_validate_json(text)
        except (ValidationError, RecursionError) as error:
            # Pydantic's normal errors may contain raw conversation data.
            raise HTTPException(422, "invalid_record") from error

    @app.exception_handler(StoreError)
    async def store_error(_request: Request, error: StoreError) -> JSONResponse:
        """Expose a stable reason without any stored payload or request arguments."""
        return JSONResponse({"detail": error.reason}, status_code=error.status)

    @app.get("/health")
    async def health() -> dict:
        """Unauthenticated liveness reveals no context, names or credentials."""
        return {"status": "ok"}

    @app.post("/v1/context-snapshots")
    async def snapshot(request: Request) -> JSONResponse:
        """Create one immutable snapshot using the publisher capability."""
        record = await body(request, publisher_hash, SnapshotInput)
        receipt = context_store.publish(record)
        return JSONResponse(receipt, status_code=200 if receipt["duplicate"] else 201)

    @app.post("/v1/tool-records")
    async def tool(request: Request) -> JSONResponse:
        """Store one tool lifecycle event under an existing live snapshot."""
        record = await body(request, publisher_hash, ToolRecordInput)
        receipt = context_store.publish(record)
        return JSONResponse(receipt, status_code=200 if receipt["duplicate"] else 201)

    @app.post("/v1/context-lookups")
    async def lookup(request: Request) -> dict:
        """Read metadata by default, or content explicitly requested by a reader."""
        query = await body(request, reader_hash, LookupInput)
        return context_store.lookup(query.sandbox_name, include_content=query.include_content)

    return app


def _credential_file(path: str) -> str:
    """Read one token file; never accept tokens on the process command line."""
    try:
        value = Path(path).read_text(encoding="utf-8").removesuffix("\n")
    except (OSError, UnicodeError) as error:
        raise ValueError("credential file could not be read") from error
    token_digest(value)
    return value


def _reject_tls_password() -> str:
    """Reject encrypted PEM keys without invoking OpenSSL's interactive prompt."""
    raise ValueError("encrypted TLS keys are unsupported")


def _validate_tls_files(cert_file: str, key_file: str) -> None:
    """Preflight a matching PEM pair without reflecting paths or OpenSSL errors."""
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_file, key_file, password=_reject_tls_password)
    except (OSError, ValueError):
        raise ValueError("TLS certificate/key could not be loaded") from None


def main() -> None:
    """Run one worker with loopback defaults, explicit credentials and TLS options."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18083)
    parser.add_argument(
        "--publisher-token-file", default=os.environ.get("NOMOS_CONTEXT_PUBLISHER_TOKEN_FILE")
    )
    parser.add_argument(
        "--reader-token-file", default=os.environ.get("NOMOS_CONTEXT_READER_TOKEN_FILE")
    )
    parser.add_argument("--ttl-seconds", type=float, default=300)
    parser.add_argument("--max-records", type=int, default=1000)
    parser.add_argument("--max-store-bytes", type=int, default=16 * 1024 * 1024)
    parser.add_argument("--max-body-bytes", type=int, default=16 * 1024)
    parser.add_argument("--read-timeout-seconds", type=float, default=1)
    parser.add_argument("--tls-cert-file")
    parser.add_argument("--tls-key-file")
    parser.add_argument("--allow-insecure-lab", action="store_true")
    args = parser.parse_args()
    try:
        if not args.publisher_token_file or not args.reader_token_file:
            raise ValueError("both publisher and reader token files are required")
        if not 1 <= args.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if bool(args.tls_cert_file) != bool(args.tls_key_file):
            raise ValueError("TLS certificate and key must be configured together")
        try:
            loopback = ipaddress.ip_address(args.host).is_loopback
        except ValueError:
            loopback = False  # DNS names are not implicitly trusted as loopback.
        if not loopback and not args.tls_cert_file and not args.allow_insecure_lab:
            raise ValueError("nonloopback binding requires TLS or explicit --allow-insecure-lab")
        if args.tls_cert_file:
            # Fail before Uvicorn starts; never prompt for a private-key password.
            _validate_tls_files(args.tls_cert_file, args.tls_key_file)
        app = create_app(
            publisher_token=_credential_file(args.publisher_token_file),
            reader_token=_credential_file(args.reader_token_file),
            store=ContextStore(
                ttl_seconds=args.ttl_seconds,
                max_records=args.max_records,
                max_bytes=args.max_store_bytes,
            ),
            max_body_bytes=args.max_body_bytes,
            read_timeout_seconds=args.read_timeout_seconds,
        )
    except ValueError as error:
        parser.error(str(error))
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        workers=1,
        access_log=False,
        proxy_headers=False,
        ws="none",
        limit_concurrency=32,
        ssl_certfile=args.tls_cert_file,
        ssl_keyfile=args.tls_key_file,
    )
