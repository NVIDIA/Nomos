# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


"""Council Chamber — FastAPI proxy + static SPA server.

The server does three things and only three things:
  1. Serve the static assets for the UI (index.html, style.css, app.js).
  2. Forward authorize/execute requests to the live council/executor.
  3. Publish the list of canned scenarios so the UI can render the rail.

No mocks, no caching, no reinterpretation of council output. The client
receives exactly what the council returns.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from base64 import b64decode
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any, ClassVar

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from nomos_runtime import SecretCheck, validate_deployment_preflight

from .council import CouncilClient
from .executor import ExecutorClient
from .scenarios import scenarios_by_id, scenarios_by_silo

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("council-chamber")

# ── Config (all overridable by env) ──────────────────────────────────

COUNCIL_URL = os.environ.get("NOMOS_COUNCIL_URL", "http://localhost:9901/mcp")
EXECUTOR_URL = os.environ.get("NOMOS_EXECUTOR_URL", "http://localhost:9907")
WORKSPACE = os.environ.get("NOMOS_WORKSPACE", "/workspace")
DEMO_AUTH_USER = os.environ.get("DEMO_AUTH_USER", "demo") or "demo"
DEMO_AUTH_PASSWORD = os.environ.get("DEMO_AUTH_PASSWORD", "")
DEMO_AUTH_REALM = os.environ.get("DEMO_AUTH_REALM", "Nomos Demo") or "Nomos Demo"

# Token registry — each seeded agent has its own cleartext token in env.
# Names line up with the pattern in deploy/.env (`NOMOS_AUTH_TOKEN_K8S_<PERSONA>`)
# with a fallback to the generic NOMOS_AUTH_TOKEN for the developer slot so
# single-token deployments still work.
AGENT_TOKENS: dict[str, str] = {
    "agent-admin": os.environ.get("NOMOS_AUTH_TOKEN_K8S_ADMIN", ""),
    "agent-developer": os.environ.get("NOMOS_AUTH_TOKEN_K8S_DEV", "")
    or os.environ.get("NOMOS_AUTH_TOKEN", ""),
    "agent-readonly": os.environ.get("NOMOS_AUTH_TOKEN_K8S_READONLY", ""),
}

# Default agent: NOMOS_AGENT_ID if set and backed by a token, otherwise the
# first configured agent (preferring admin for broadest scenario coverage).
_env_default = os.environ.get("NOMOS_AGENT_ID", "")
if _env_default and AGENT_TOKENS.get(_env_default):
    DEFAULT_AGENT_ID = _env_default
else:
    DEFAULT_AGENT_ID = next(
        (
            aid
            for aid in ("agent-admin", "agent-developer", "agent-readonly")
            if AGENT_TOKENS.get(aid)
        ),
        "agent-developer",
    )

_configured_agents = [aid for aid, tok in AGENT_TOKENS.items() if tok]
if not _configured_agents:
    logger.warning(
        "No agent tokens configured. Set NOMOS_AUTH_TOKEN_K8S_ADMIN, "
        "NOMOS_AUTH_TOKEN_K8S_DEV, or NOMOS_AUTH_TOKEN_K8S_READONLY (or the "
        "generic NOMOS_AUTH_TOKEN) before starting the demo — council_authorize "
        "will fail without a real token."
    )
else:
    logger.info("Configured agents: %s (default: %s)", _configured_agents, DEFAULT_AGENT_ID)

council = CouncilClient(COUNCIL_URL)
executor = ExecutorClient(EXECUTOR_URL)


def _validate_demo_deployment_preflight(*, auth_password: str) -> None:
    validate_deployment_preflight(
        profile=os.environ.get("NOMOS_DEPLOY_PROFILE"),
        secrets=[
            SecretCheck("DEMO_AUTH_PASSWORD", auth_password),
        ],
    )


_validate_demo_deployment_preflight(auth_password=DEMO_AUTH_PASSWORD)


def _scrub(value: object) -> str:
    """Neutralize CR/LF in user-controlled values before logging.

    Prevents log forging/injection when request-supplied data is written to
    the log (sonar S5145).
    """
    return str(value).replace("\r", "\\r").replace("\n", "\\n")


def _token_for(agent_id: str) -> str:
    tok = AGENT_TOKENS.get(agent_id, "")
    if not tok:
        raise HTTPException(
            status_code=400,
            detail=(
                f"No auth token configured for agent {agent_id!r}. "
                f"Configured agents: {_configured_agents or 'none'}."
            ),
        )
    return tok


# ── Sessions ─────────────────────────────────────────────────────────
#
# Cosmetic session model: every browser that hits the demo is given a
# short opaque cookie so the UI can show a "session: XXXX" badge and so
# that concurrent runs are attributable in the server log. The backend
# is already concurrency-safe — this is not an auth boundary.

SESSION_COOKIE = "council_demo_session"
SESSION_IDLE_TIMEOUT_SECONDS = 24 * 60 * 60  # drop sessions idle > 24h
SESSION_SWEEP_INTERVAL_SECONDS = 5 * 60


@dataclass
class SessionState:
    session_id: str
    created_at: float
    last_seen: float
    preferred_agent_id: str = ""
    run_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


_SESSIONS: dict[str, SessionState] = {}
_SESSIONS_LOCK = Lock()
_LAST_SWEEP = 0.0


def _sweep_idle_sessions(now: float) -> None:
    """Drop sessions that haven't been touched within the idle timeout."""
    global _LAST_SWEEP
    if now - _LAST_SWEEP < SESSION_SWEEP_INTERVAL_SECONDS:
        return
    _LAST_SWEEP = now
    cutoff = now - SESSION_IDLE_TIMEOUT_SECONDS
    with _SESSIONS_LOCK:
        stale = [sid for sid, s in _SESSIONS.items() if s.last_seen < cutoff]
        for sid in stale:
            _SESSIONS.pop(sid, None)
    if stale:
        logger.info("session sweep: dropped %d idle session(s)", len(stale))


def _get_or_create_session(session_id: str | None) -> tuple[SessionState, bool]:
    now = time.time()
    _sweep_idle_sessions(now)
    with _SESSIONS_LOCK:
        if session_id and session_id in _SESSIONS:
            s = _SESSIONS[session_id]
            s.last_seen = now
            return s, False
        new_id = secrets.token_urlsafe(9)  # ~12 chars, e.g. "Xa3f1Q_2bK9"
        while new_id in _SESSIONS:  # collision guard (astronomically rare)
            new_id = secrets.token_urlsafe(9)
        s = SessionState(session_id=new_id, created_at=now, last_seen=now)
        _SESSIONS[new_id] = s
        return s, True


class SessionMiddleware(BaseHTTPMiddleware):
    """Attach a SessionState to every request; set the cookie if new."""

    async def dispatch(self, request: Request, call_next):
        incoming = request.cookies.get(SESSION_COOKIE)
        session, is_new = _get_or_create_session(incoming)
        request.state.session = session
        response = await call_next(request)
        if is_new or incoming != session.session_id:
            # HttpOnly to keep JS from rifling through it; the UI reads the
            # id from /api/config, not the cookie. SameSite=Lax so navigation
            # back into the demo still carries it.
            response.set_cookie(
                SESSION_COOKIE,
                session.session_id,
                max_age=SESSION_IDLE_TIMEOUT_SECONDS,
                httponly=True,
                samesite="lax",
            )
        return response


# ── Static asset paths ───────────────────────────────────────────────

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE.parent / "static"

# ── Request schemas ──────────────────────────────────────────────────


class AuthorizeRequest(BaseModel):
    """Either `scenario_id` OR the full intent fields are required.

    `action_type` is deliberately absent — it's no longer supplied by
    agents. The council derives required permissions from the command.
    """

    scenario_id: str | None = None
    agent_id: str | None = None  # override; otherwise scenario default or server default

    command: str = Field(default="")
    description: str = Field(default="")
    target_resource: str = Field(default="")
    justification: str = Field(default="")
    blast_radius: str = Field(default="")
    target_environment: str = Field(default="development")
    working_directory: str | None = Field(default=None)
    certified_evidence: list[dict[str, Any]] = Field(default_factory=list)
    packaged_files: list[dict[str, Any]] = Field(default_factory=list)


class ExecuteRequest(BaseModel):
    writ: str
    agent_id: str | None = None  # must match the writ's target_agent_id


# ── App ──────────────────────────────────────────────────────────────

app = FastAPI(title="Council Chamber", docs_url=None, redoc_url=None)


class DemoAuthMiddleware(BaseHTTPMiddleware):
    """Require Basic auth for the demo UI and API while leaving probes public."""

    _PUBLIC_PATHS: ClassVar[set[str]] = {"/api/health"}

    @staticmethod
    def _challenge() -> dict[str, str]:
        return {"WWW-Authenticate": f'Basic realm="{DEMO_AUTH_REALM}", charset="UTF-8"'}

    @staticmethod
    def _credentials(request: Request) -> tuple[str, str] | None:
        auth = request.headers.get("Authorization", "")
        scheme, _, encoded = auth.partition(" ")
        if scheme.lower() != "basic" or not encoded:
            return None
        try:
            decoded = b64decode(encoded, validate=True).decode("utf-8")
        except Exception:
            return None
        user, sep, password = decoded.partition(":")
        if not sep:
            return None
        return user, password

    async def dispatch(self, request: Request, call_next):
        if request.url.path in self._PUBLIC_PATHS:
            return await call_next(request)

        if not DEMO_AUTH_PASSWORD:
            return PlainTextResponse("Demo password is not configured.", status_code=503)

        credentials = self._credentials(request)
        if credentials is None:
            return PlainTextResponse(
                "Authentication required.",
                status_code=401,
                headers=self._challenge(),
            )

        user, password = credentials
        user_ok = secrets.compare_digest(user, DEMO_AUTH_USER)
        password_ok = secrets.compare_digest(password, DEMO_AUTH_PASSWORD)
        if not (user_ok and password_ok):
            return PlainTextResponse(
                "Authentication required.",
                status_code=401,
                headers=self._challenge(),
            )

        return await call_next(request)


# Development-friendly: any /static/* (and the root index) should never be
# browser-cached. Without this, edits to CSS / JS / the SPA shell can stay
# invisible for hours behind aggressive browser + ES-module caching.
class NoStoreStaticMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response


app.add_middleware(NoStoreStaticMiddleware)
app.add_middleware(SessionMiddleware)
app.add_middleware(DemoAuthMiddleware)


@app.get("/api/config")
async def config(request: Request) -> dict[str, Any]:
    """Connection info so the UI banner can state what it's pointed at."""
    session: SessionState = request.state.session
    # If this session has already expressed a preferred agent, advertise that
    # as the default so a reload lands on the same dropdown selection.
    default_agent = session.preferred_agent_id or DEFAULT_AGENT_ID
    return {
        "council_url": COUNCIL_URL,
        "executor_url": EXECUTOR_URL,
        "default_agent_id": default_agent,
        "agents": [{"id": aid, "has_token": bool(tok)} for aid, tok in AGENT_TOKENS.items()],
        "workspace": WORKSPACE,
        "session": {
            "id": session.session_id,
            "created_at": session.created_at,
            "run_count": session.run_count,
        },
    }


class SessionAgentRequest(BaseModel):
    agent_id: str = Field(default="")


@app.post("/api/session/agent")
async def set_session_agent(req: SessionAgentRequest, request: Request) -> dict[str, Any]:
    """Remember the user's agent selection for the lifetime of this session."""
    session: SessionState = request.state.session
    if req.agent_id and req.agent_id not in AGENT_TOKENS:
        raise HTTPException(status_code=400, detail=f"Unknown agent {req.agent_id!r}")
    session.preferred_agent_id = req.agent_id
    return {"session_id": session.session_id, "preferred_agent_id": session.preferred_agent_id}


@app.get("/api/scenarios")
async def scenarios() -> dict[str, Any]:
    """Scenarios grouped by silo (job role) for the rail."""
    return {"silos": scenarios_by_silo()}


@app.get("/api/health")
async def health() -> dict[str, Any]:
    """Best-effort liveness check for council + executor."""
    out: dict[str, Any] = {"council": {"ok": False}, "executor": {"ok": False}}
    try:
        pk = await council.get_public_key()
        out["council"] = {"ok": True, "public_key_prefix": (pk.get("public_key") or "")[:16]}
    except Exception as exc:
        out["council"] = {"ok": False, "error": str(exc)}
    try:
        h = await executor.health()
        out["executor"] = {
            "ok": h.get("status") == "ok",
            "key_fingerprint": (h.get("key_fingerprint") or "")[:16],
            "uptime_seconds": h.get("uptime_seconds"),
        }
    except Exception as exc:
        out["executor"] = {"ok": False, "error": str(exc)}
    return out


@app.post("/api/authorize")
async def authorize(req: AuthorizeRequest, request: Request) -> JSONResponse:
    """Forward to the live council_authorize MCP tool."""
    session: SessionState = request.state.session
    scenario_agent_id: str | None = None
    if req.scenario_id:
        sc = scenarios_by_id().get(req.scenario_id)
        if sc is None:
            raise HTTPException(status_code=404, detail=f"Unknown scenario {req.scenario_id!r}")
        intent = sc.to_intent()
        scenario_agent_id = sc.agent_id
    else:
        intent = {
            "command": req.command,
            "description": req.description,
            "target_resource": req.target_resource,
            "justification": req.justification,
            "blast_radius": req.blast_radius,
            "target_environment": req.target_environment,
        }
        if req.packaged_files:
            intent["packaged_files"] = req.packaged_files

    certified_evidence_count = len(req.certified_evidence)
    if req.certified_evidence:
        intent["certified_evidence"] = req.certified_evidence
    intent["working_directory"] = req.working_directory or WORKSPACE

    # Agent resolution: request override > scenario default > server default.
    agent_id = req.agent_id or scenario_agent_id or DEFAULT_AGENT_ID
    token = _token_for(agent_id)

    logger.info(
        "authorize: session=%s agent=%s command=%s environment=%s certified_evidence=%d",
        session.session_id,
        _scrub(agent_id),
        _scrub(intent.get("command", "")[:120]),
        _scrub(intent.get("target_environment")),
        certified_evidence_count,
    )
    try:
        result = await council.authorize(agent_id, token, intent)
    except Exception as exc:
        logger.exception("council_authorize failed (session=%s)", session.session_id)
        raise HTTPException(status_code=502, detail=f"council_authorize failed: {exc}") from exc
    session.run_count += 1

    # Echo the agent_id back so the UI can send the same value to /api/execute
    # (the executor expects X-Agent-ID to match writ.target_agent_id).
    result.setdefault("_agent_id", agent_id)
    result.setdefault("_certified_evidence_count", certified_evidence_count)
    return JSONResponse(result)


@app.post("/api/execute")
async def execute(req: ExecuteRequest, request: Request) -> JSONResponse:
    """Forward a signed writ to the live executor."""
    session: SessionState = request.state.session
    if not req.writ:
        raise HTTPException(status_code=400, detail="writ is required")
    agent_id = req.agent_id or DEFAULT_AGENT_ID
    # We don't need a token for the executor call — the writ carries its own
    # authorization. We only need to send the right X-Agent-ID header.
    logger.info("execute: session=%s agent=%s", session.session_id, _scrub(agent_id))
    try:
        result = await executor.execute(req.writ, agent_id)
    except Exception as exc:
        logger.exception("executor contract call failed (session=%s)", session.session_id)
        raise HTTPException(status_code=502, detail=f"executor call failed: {exc}") from exc
    return JSONResponse(result)


# ── Static routes (mounted last so API routes take precedence) ──────

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def root() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
