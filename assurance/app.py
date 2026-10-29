# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Opt-in assurance service with protected results and independent stream workers."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hmac
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field, model_validator

from assurance.monitor import LedgerSource, Monitor, Store
from assurance.scheduler import StreamWorker, public_run

logger = logging.getLogger(__name__)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    if value.lower() not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value.lower() == "true"


class Settings(BaseModel):
    enabled: bool = False
    ethos_enabled: bool = False
    export_enabled: bool = False
    evaluation_export_enabled: bool = False
    export_redact_values: list[str] = Field(default_factory=list, repr=False)
    source_url: str = ""
    council_id: str = ""
    chain_id: str = ""
    source_token: str = Field(default="", repr=False)
    username: str = ""
    password: str = Field(default="", repr=False)
    api_read_token: str = Field(default="", repr=False)
    keys: dict[str, str] = Field(default_factory=dict)
    state_path: Path = Path("/state/monitor.db")
    poll_seconds: float = Field(default=2, ge=0.1, le=3600)
    analysis_seconds: float = Field(default=300, ge=0.1, le=86400)
    stream_timeout: float = Field(default=120, ge=0.1, le=3600)
    max_backlog: int = Field(default=10000, ge=200, le=1000000)
    max_attempts: int = Field(default=3, ge=1, le=10)
    ethos_config_file: Path | None = None
    model: str = ""
    api_base: str = ""
    api_key: str = Field(default="", repr=False)
    inference_timeout: float = Field(default=60, ge=0.1, le=300)
    max_tokens: int = Field(default=4096, ge=64, le=32768)
    max_model_calls: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def required_when_enabled(self):
        if not self.enabled:
            if self.ethos_enabled or self.export_enabled or self.evaluation_export_enabled:
                raise ValueError("enable assurance before enabling ETHOS or export")
            return self
        if sys.version_info[:2] not in {(3, 12), (3, 13)}:
            raise ValueError("enabled assurance requires Python 3.12 or 3.13")
        for field in (
            "source_url",
            "council_id",
            "chain_id",
            "source_token",
            "username",
            "password",
            "api_read_token",
        ):
            if not getattr(self, field).strip():
                raise ValueError(f"{field} is required when assurance is enabled")
        if not self.keys or any(
            len(value) != 64 or any(c not in "0123456789abcdefABCDEF" for c in value)
            for value in self.keys.values()
        ):
            raise ValueError("trusted Ed25519 public keys must be configured")
        if not self.source_url.startswith(("http://", "https://")):
            raise ValueError("source_url must be an HTTP(S) origin")
        if self.ethos_enabled:
            if self.ethos_config_file is None:
                raise ValueError("ETHOS_CONFIG_FILE is required for ETHOS")
            for field in ("model", "api_base", "api_key"):
                if not getattr(self, field).strip():
                    raise ValueError(f"{field} is required for ETHOS")
        return self

    @classmethod
    def from_env(cls):
        enabled = env_bool("ASSURANCE_ENABLED")
        ethos_enabled = env_bool("ASSURANCE_ETHOS_ENABLED")
        export_enabled = env_bool("ASSURANCE_EXPORT_ENABLED")
        evaluation_export_enabled = env_bool("ASSURANCE_EVALUATION_EXPORT_ENABLED")
        export_redact_values = json.loads(os.environ.get("ASSURANCE_EXPORT_REDACT_VALUES", "[]"))
        if not enabled:
            return cls(
                enabled=False,
                ethos_enabled=ethos_enabled,
                export_enabled=export_enabled,
                evaluation_export_enabled=evaluation_export_enabled,
            )
        keys_file = os.environ.get("TRUSTED_KEYS_FILE")
        if not keys_file:
            raise ValueError("TRUSTED_KEYS_FILE is required when assurance is enabled")
        return cls(
            enabled=True,
            ethos_enabled=ethos_enabled,
            export_enabled=export_enabled,
            evaluation_export_enabled=evaluation_export_enabled,
            export_redact_values=export_redact_values,
            source_url=os.environ.get("SOURCE_URL", ""),
            council_id=os.environ.get("COUNCIL_ID", ""),
            chain_id=os.environ.get("CHAIN_ID", ""),
            source_token=os.environ.get("SOURCE_TOKEN", ""),
            username=os.environ.get("DASHBOARD_USER", ""),
            password=os.environ.get("DASHBOARD_PASSWORD", ""),
            api_read_token=os.environ.get("API_READ_TOKEN", ""),
            keys=json.loads(Path(keys_file).read_text()),
            state_path=Path(os.environ.get("STATE_PATH", "/state/monitor.db")),
            poll_seconds=float(os.environ.get("POLL_SECONDS", "2")),
            analysis_seconds=float(os.environ.get("ANALYSIS_SECONDS", "300")),
            stream_timeout=float(os.environ.get("ASSURANCE_STREAM_TIMEOUT", "120")),
            max_backlog=int(os.environ.get("ASSURANCE_MAX_BACKLOG", "10000")),
            ethos_config_file=Path(os.environ["ETHOS_CONFIG_FILE"])
            if ethos_enabled and os.environ.get("ETHOS_CONFIG_FILE")
            else None,
            model=os.environ.get("ASSURANCE_MODEL", ""),
            api_base=os.environ.get("ASSURANCE_API_BASE", ""),
            api_key=os.environ.get("ASSURANCE_API_KEY", ""),
            inference_timeout=float(os.environ.get("ASSURANCE_INFERENCE_TIMEOUT", "60")),
            max_tokens=int(os.environ.get("ASSURANCE_MAX_TOKENS", "4096")),
            max_model_calls=int(os.environ.get("ASSURANCE_MAX_MODEL_CALLS", "20")),
        )


class EvaluationSelection(BaseModel):
    model_config = {"extra": "forbid"}
    finding_ids: list[str] = Field(min_length=1, max_length=20)
    subject: Literal["council-decision", "client-agent"]


class EvaluationDownload(BaseModel):
    model_config = {"extra": "forbid"}
    capture_id: str = Field(pattern=r"^[0-9a-f]{64}$")


def create_app(settings: Settings | None = None, *, run_worker: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    if not settings.enabled:
        return FastAPI(title="Assurance disabled", docs_url=None, redoc_url=None, openapi_url=None)
    # All optional Compass/inference imports are below the explicit opt-in gate.
    from assurance.analysis import COMPASS_COMMIT, EVIDENCE_ADAPTER_VERSION, analyze

    os.umask(0o077)
    store = Store(settings.state_path, settings.chain_id)
    workers = [
        StreamWorker(
            store,
            "tool-issues",
            analyze,
            config={"compass_commit": COMPASS_COMMIT, "adapter": EVIDENCE_ADAPTER_VERSION},
            interval=settings.analysis_seconds,
            timeout=settings.stream_timeout,
            max_attempts=settings.max_attempts,
        )
    ]
    if settings.ethos_enabled:
        from assurance.ethos import (
            ETHOS_ADAPTER_VERSION,
            EthosRegistry,
            InferenceConfig,
            analyze_ethos,
        )

        registry = EthosRegistry.from_file(settings.ethos_config_file)
        inference = InferenceConfig(
            model=settings.model,
            api_base=settings.api_base,
            api_key=settings.api_key,
            max_tokens=settings.max_tokens,
            request_timeout=settings.inference_timeout,
            max_calls=settings.max_model_calls,
        )

        async def ethos_analyzer(events, chain_id):
            return await analyze_ethos(events, chain_id, registry, inference, cache=store)

        artifacts = {
            "manifest": registry.manifest(),
            "documents": {
                name: body.decode("utf-8") for name, body in registry.export_documents().items()
            },
        }
        workers.append(
            StreamWorker(
                store,
                "ethos-divergence",
                ethos_analyzer,
                config={
                    "compass_commit": COMPASS_COMMIT,
                    "adapter": ETHOS_ADAPTER_VERSION,
                    "evidence_adapter": EVIDENCE_ADAPTER_VERSION,
                    "ethos_digest": registry.digest,
                    "inference": inference.manifest(),
                },
                artifacts=artifacts,
                interval=settings.analysis_seconds,
                timeout=settings.stream_timeout,
                max_attempts=settings.max_attempts,
            )
        )
    if settings.export_enabled:
        root = Path(os.environ.get("EVAL_AUTHOR_ROOT", "/opt/eval-author"))
        if not (root / "skills/eval-author-audit/scripts/audit_spec/validate.py").is_file():
            raise ValueError("audit export requires the pinned Eval Author validator")

    captures = None
    if settings.evaluation_export_enabled:
        from assurance.author_export import CaptureCache

        captures = CaptureCache()

    async def ingestion():
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
                    await monitor.cycle(
                        backlog=max(
                            store.checkpoint()[0] - worker.progress() for worker in workers
                        ),
                        max_backlog=settings.max_backlog,
                    )
                except Exception as exc:
                    logger.warning("assurance ingestion failed: %s", type(exc).__name__)
                await asyncio.sleep(settings.poll_seconds)

    async def analysis_loop(worker):
        while True:
            try:
                await worker.step()
            except Exception as exc:
                logger.warning("assurance stream %s failed: %s", worker.stream, type(exc).__name__)
            await asyncio.sleep(min(1, settings.poll_seconds))

    @asynccontextmanager
    async def lifespan(app):
        tasks, lock = [], None
        if run_worker:
            lock = settings.state_path.with_suffix(".lock").open("a")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                lock.close()
                raise RuntimeError("another assurance observer owns this state") from None
            tasks = [
                asyncio.create_task(ingestion()),
                *(asyncio.create_task(analysis_loop(worker)) for worker in workers),
            ]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if lock:
                lock.close()

    app = FastAPI(
        title="Nomos asynchronous assurance",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.store, app.state.workers = store, workers
    security = HTTPBasic(auto_error=False)

    def basic_ok(credentials):
        return (
            credentials is not None
            and hmac.compare_digest(credentials.username.encode(), settings.username.encode())
            and hmac.compare_digest(credentials.password.encode(), settings.password.encode())
        )

    def authenticate(
        credentials: HTTPBasicCredentials | None = Depends(security),
        authorization: str = Header(default=""),
    ):
        token_ok = hmac.compare_digest(
            authorization.encode(), f"Bearer {settings.api_read_token}".encode()
        )
        if not (basic_ok(credentials) or token_ok):
            raise HTTPException(
                401, "Authentication required", headers={"WWW-Authenticate": "Basic"}
            )

    def operator_guard(operation: str):
        def guard(
            credentials: HTTPBasicCredentials | None = Depends(security),
            x_assurance_operator: str = Header(default=""),
            sec_fetch_site: str | None = Header(default=None),
        ):
            if not basic_ok(credentials):
                raise HTTPException(401, "Operator authentication required")
            if x_assurance_operator != operation or sec_fetch_site not in {None, "same-origin"}:
                raise HTTPException(403, "Same-origin operator header required")

        return guard

    operator = operator_guard("retry")
    export_operator = operator_guard("evaluation-export")

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; "
                    "style-src 'self'; frame-ancestors 'none'"
                ),
            }
        )
        return response

    @app.get("/healthz")
    def health():
        return {"status": "up"}

    @app.get("/api/status", dependencies=[Depends(authenticate)])
    def status():
        seq, content_hash = store.checkpoint()
        streams = {worker.stream: worker.status() for worker in workers}
        streams.setdefault("ethos-divergence", {"enabled": False, "status": "disabled"})
        return {
            "chain_id": store.chain_id,
            "poll_seconds": settings.poll_seconds,
            "analysis_seconds": settings.analysis_seconds,
            "ethos_enabled": settings.ethos_enabled,
            "checkpoint": {"sequence": seq, "content_hash": content_hash},
            "analysis_sequence": store.analysis_sequence(),
            "worker": store.setting("status", {"state": "starting"}),
            "coverage": store.setting("coverage", {}),
            "streams": streams,
            "backlog": {w.stream: max(0, seq - w.progress()) for w in workers},
            "export_enabled": settings.export_enabled,
            "evaluation_export_enabled": settings.evaluation_export_enabled,
            "enforcement": "none; advisory only",
        }

    def page(items, total, limit, offset, key="items"):
        return {
            key: items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "next_offset": offset + limit if offset + limit < total else None,
        }

    def bound(value):
        if value is None:
            return ""
        if value.tzinfo is None:
            raise HTTPException(422, "time filters require a timezone")
        return value.astimezone(UTC).isoformat()

    @app.get("/api/findings", dependencies=[Depends(authenticate)])
    def findings(
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        agent: str = "",
        stream: str = "",
        run_id: str = "",
        kind: str = "",
        disposition: str = "",
        since: datetime | None = None,
        until: datetime | None = None,
    ):
        items, total = store.query_findings(
            limit,
            offset,
            agent=agent,
            stream=stream,
            run_id=run_id,
            kind=kind,
            disposition=disposition,
            since=bound(since),
            until=bound(until),
        )
        return page(items, total, limit, offset, "findings")

    @app.get("/api/findings/{finding_id}", dependencies=[Depends(authenticate)])
    def get_finding(finding_id: str):
        item = store.get_finding(finding_id)
        if item is None:
            raise HTTPException(404, "Finding not found")
        return item

    @app.get("/api/runs", dependencies=[Depends(authenticate)])
    def runs(
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        stream: str = "",
        status: str = "",
        agent: str = "",
        since: datetime | None = None,
        until: datetime | None = None,
    ):
        filters = {
            "stream": stream,
            "status": status,
            "agent": agent,
            "since": bound(since),
            "until": bound(until),
        }
        return page(
            [public_run(r) for r in store.runs(limit, offset, **filters)],
            store.count_runs(**filters),
            limit,
            offset,
        )

    @app.get("/api/runs/{run_id}", dependencies=[Depends(authenticate)])
    def get_run(run_id: str):
        run = store.run(run_id)
        if run is None:
            raise HTTPException(404, "Run not found")
        return public_run(run)

    @app.post("/api/runs/{run_id}/retry", dependencies=[Depends(operator)])
    def retry(run_id: str):
        run = store.run(run_id)
        if run is None:
            raise HTTPException(404, "Run not found")
        worker = next((w for w in workers if w.stream == run["stream"]), None)
        try:
            if worker is None:
                raise ValueError("stream is disabled")
            worker.retry(run)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"status": "pending", "run_id": run_id}

    @app.get("/api/export", dependencies=[Depends(authenticate)])
    def export():
        if not settings.export_enabled:
            raise HTTPException(404, "Audit export is disabled")
        from assurance.export import build_bundle

        try:
            bundle = build_bundle(store)
        except Exception as exc:
            logger.warning("assurance export failed: %s", type(exc).__name__)
            raise HTTPException(503, "Audit export unavailable; validator must succeed") from None
        return Response(
            bundle,
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="nomos-eval-handoff.zip"'},
        )

    @app.post("/api/evaluation-export/preview", dependencies=[Depends(export_operator)])
    def evaluation_preview(selection: EvaluationSelection):
        if captures is None:
            raise HTTPException(404, "Evaluation authoring export is disabled")
        from assurance.author_export import ExportError

        try:
            preview = captures.prepare(
                store,
                selection.finding_ids,
                selection.subject,
                [
                    settings.source_token,
                    settings.password,
                    settings.api_read_token,
                    settings.api_key,
                    *settings.export_redact_values,
                ],
            )
        except ExportError as exc:
            raise HTTPException(422, str(exc)) from None
        except Exception as exc:
            logger.warning("evaluation capture failed: %s", type(exc).__name__)
            raise HTTPException(503, "Evaluation capture unavailable") from None
        logger.info(
            "evaluation export prepared capture=%s findings=%d",
            preview["capture_id"],
            len(selection.finding_ids),
        )
        return preview

    @app.post("/api/evaluation-export", dependencies=[Depends(export_operator)])
    def evaluation_download(selection: EvaluationDownload):
        if captures is None:
            raise HTTPException(404, "Evaluation authoring export is disabled")
        from assurance.author_export import ExportError

        try:
            bundle = captures.download(selection.capture_id)
        except ExportError as exc:
            raise HTTPException(409, str(exc)) from None
        logger.info("evaluation export downloaded capture=%s", selection.capture_id)
        return Response(
            bundle,
            media_type="application/zip",
            headers={
                "Content-Disposition": 'attachment; filename="nomos-evaluation-authoring.zip"',
            },
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
