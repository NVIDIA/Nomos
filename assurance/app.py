# SPDX-License-Identifier: Apache-2.0
"""Authenticated observer UI/API with an independent polling task."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from assurance.export import build_bundle
from assurance.monitor import LedgerSource, Monitor, Store

logger = logging.getLogger(__name__)


class Settings(BaseModel):
    source_url: str = Field(min_length=1)
    council_id: str = Field(min_length=1)
    chain_id: str = Field(min_length=1)
    source_token: str = Field(min_length=1, repr=False)
    username: str = Field(min_length=1)
    password: str = Field(min_length=1, repr=False)
    keys: dict[str, str] = Field(min_length=1)
    state_path: Path
    poll_seconds: float = Field(default=2, ge=0.1, le=3600)

    @classmethod
    def from_env(cls):
        return cls(
            source_url=os.environ["SOURCE_URL"],
            council_id=os.environ["COUNCIL_ID"],
            chain_id=os.environ["CHAIN_ID"],
            source_token=os.environ["SOURCE_TOKEN"],
            username=os.environ["DASHBOARD_USER"],
            password=os.environ["DASHBOARD_PASSWORD"],
            keys=json.loads(Path(os.environ["TRUSTED_KEYS_FILE"]).read_text()),
            state_path=Path(os.environ.get("STATE_PATH", "/state/monitor.db")),
            poll_seconds=float(os.environ.get("POLL_SECONDS", "2")),
        )


def create_app(settings: Settings | None = None, *, run_worker: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    os.umask(0o077)
    store = Store(settings.state_path, settings.chain_id)

    async def worker():
        async with httpx.AsyncClient(trust_env=False) as client:
            monitor = Monitor(
                LedgerSource(
                    settings.source_url, settings.council_id, settings.source_token, client=client
                ),
                store,
                settings.keys,
            )
            while True:
                try:
                    await monitor.cycle()
                except Exception as exc:
                    logger.warning("assurance poll failed: %s", type(exc).__name__)
                await asyncio.sleep(settings.poll_seconds)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(worker()) if run_worker else None
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(
        title="Nomos asynchronous assurance PoC",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    security = HTTPBasic()

    def authenticate(credentials: HTTPBasicCredentials = Depends(security)):
        user_ok = hmac.compare_digest(credentials.username.encode(), settings.username.encode())
        pass_ok = hmac.compare_digest(credentials.password.encode(), settings.password.encode())
        if not (user_ok and pass_ok):
            raise HTTPException(
                401, "Authentication required", headers={"WWW-Authenticate": "Basic"}
            )

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'"
        )
        return response

    @app.get("/healthz")
    def health():
        return {"status": "up"}

    @app.get("/api/status", dependencies=[Depends(authenticate)])
    def status():
        seq, digest = store.checkpoint()
        return {
            "chain_id": store.chain_id,
            "poll_seconds": settings.poll_seconds,
            "checkpoint": {"sequence": seq, "content_hash": digest},
            "analysis_sequence": store.analysis_sequence(),
            "worker": store.setting("status", {"state": "starting"}),
            "coverage": store.setting("coverage", {}),
            "enforcement": "none; advisory only",
        }

    @app.get("/api/findings", dependencies=[Depends(authenticate)])
    def findings():
        return {"findings": store.findings(), "limit": 1000}

    @app.get("/api/export", dependencies=[Depends(authenticate)])
    def export():
        try:
            bundle = build_bundle(store)
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            raise HTTPException(503, "Audit export unavailable; validator must succeed") from exc
        return Response(
            bundle,
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="nomos-eval-handoff.zip"'},
        )

    @app.get("/assets/{name}", dependencies=[Depends(authenticate)])
    def asset(name: str):
        types = {
            "dashboard.css": "text/css",
            "dashboard.mjs": "application/javascript",
            "dashboard-model.mjs": "application/javascript",
        }
        if name not in types:
            raise HTTPException(404, "Asset not found")
        return Response(Path(__file__).with_name(name).read_text(), media_type=types[name])

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(authenticate)])
    def index():
        return Path(__file__).with_name("dashboard.html").read_text()

    return app
