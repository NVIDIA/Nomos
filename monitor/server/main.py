# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Council MCP monitor — FastAPI app.

Single ASGI app that fronts the council MCP server: it accepts incoming
MCP traffic on `/mcp`, forwards it to the configured upstream, and serves
a small dashboard at `/` plus JSON metrics at `/api/metrics` and
`/api/recent`.

Why one process: the proxy and the dashboard both need access to the
same in-memory metrics registry. Splitting them would require an extra
scrape hop and complicate deployment without buying anything.
"""

from __future__ import annotations

import base64
import csv
import hmac
import io
import logging
import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from monitor.server.ledger import get_ledger_entry, load_ledger_page, load_trusted_public_keys
from monitor.server.ledger_api import get_ledger_entry_from_api, load_ledger_page_from_api
from monitor.server.metrics import MetricsRegistry, Snapshot
from monitor.server.proxy import forward_mcp
from monitor.server.redis_stats import RedisStatsReader
from nomos_runtime import DeploymentPreflightError, SecretCheck, validate_deployment_preflight

# nomos_runtime.redis_client (and `redis`) are imported lazily in
# _build_stats_reader — the lean monitor image only ships `redis` for the
# opt-in stats path, so a top-level import would crash the default build.

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("council-monitor")

DEFAULT_UPSTREAM = "http://localhost:9901/mcp"
DEFAULT_HEALTH_PATH = "/debug/tools/list"
DEFAULT_DASHBOARD_USER = "admin"
HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE.parent / "static"


def _derive_health_url(upstream_mcp_url: str) -> str:
    """Use the council's `/debug/tools/list` endpoint for upstream liveness.

    It's the cheapest non-auth call the council exposes and shares the same
    host as `/mcp`, so we can derive it from the configured upstream.
    """
    base = upstream_mcp_url.rsplit("/mcp", 1)[0] or upstream_mcp_url
    return base.rstrip("/") + DEFAULT_HEALTH_PATH


def _build_stats_reader(window_seconds: float, history_limit: int) -> RedisStatsReader | None:
    """Build the Redis stats reader when MONITOR_STATS_SOURCE=redis.

    Mirrors the council/executor MONITOR_REDIS_* topology contract (sentinel-aware).
    Returns None for the default 'inline' source (in-memory registry) or on a
    misconfiguration — the dashboard then falls back to the registry.
    """
    source = os.environ.get("MONITOR_STATS_SOURCE", "inline").strip().lower()
    if source != "redis":
        return None
    try:
        from nomos_runtime.redis_client import RedisConnectionConfig, create_redis_client
    except ImportError as exc:
        logger.warning("MONITOR_STATS_SOURCE=redis but `redis` is not installed: %s", exc)
        return None
    nodes = tuple(_split_env_list(os.environ.get("MONITOR_REDIS_SENTINEL_NODES", "")))
    config = RedisConnectionConfig(
        mode=os.environ.get("MONITOR_REDIS_MODE", "standalone").strip() or "standalone",
        host=os.environ.get("MONITOR_REDIS_HOST", "localhost"),
        port=int(os.environ.get("MONITOR_REDIS_PORT", "6379")),
        password=os.environ.get("MONITOR_REDIS_PASSWORD", ""),
        sentinel_service_name=os.environ.get("MONITOR_REDIS_SENTINEL_SERVICE_NAME", ""),
        sentinel_nodes=nodes,
        sentinel_password=os.environ.get("MONITOR_REDIS_SENTINEL_PASSWORD", ""),
    )
    stream = os.environ.get("MONITOR_STATS_STREAM", "nomos:council:telemetry")
    try:
        client = create_redis_client(config)
    except Exception as exc:  # bad config must not crash startup
        logger.warning("MONITOR_STATS_SOURCE=redis but client init failed: %s", exc)
        return None
    logger.info("monitor stats source: redis stream %s (mode=%s)", stream, config.mode)
    return RedisStatsReader(
        client, stream=stream, window_seconds=window_seconds, history_limit=history_limit
    )


class NoStoreStaticMiddleware(BaseHTTPMiddleware):
    """Same dev-friendly cache headers Chamber uses for its SPA shell."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response


class DashboardBasicAuthMiddleware(BaseHTTPMiddleware):
    """Protect the browser dashboard without blocking MCP or health traffic."""

    async def dispatch(self, request: Request, call_next):
        if not _is_dashboard_path(request.url.path):
            return await call_next(request)

        password = getattr(request.app.state, "dashboard_password", "")
        if not password:
            return await call_next(request)

        expected_user = getattr(request.app.state, "dashboard_user", DEFAULT_DASHBOARD_USER)
        if _has_valid_basic_auth(
            request.headers.get("authorization", ""),
            expected_user=expected_user,
            expected_password=password,
        ):
            return await call_next(request)

        return _basic_auth_challenge()


def _is_dashboard_path(path: str) -> bool:
    return path in {"/", "/api", "/static"} or path.startswith(("/api/", "/static/"))


def _has_valid_basic_auth(
    authorization: str,
    *,
    expected_user: str,
    expected_password: str,
) -> bool:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "basic" or not token:
        return False
    try:
        decoded = base64.b64decode(token, validate=True).decode("utf-8")
    except Exception:
        return False
    user, sep, password = decoded.partition(":")
    if not sep:
        return False
    return hmac.compare_digest(user, expected_user) and hmac.compare_digest(
        password,
        expected_password,
    )


def _basic_auth_challenge() -> Response:
    return Response(
        status_code=401,
        headers={
            "WWW-Authenticate": 'Basic realm="Nomos Monitor", charset="UTF-8"',
            "Cache-Control": "no-store",
        },
    )


def _validate_monitor_deployment_preflight(
    *,
    internal_token: str,
    dashboard_password: str,
    mcp_proxy_token: str,
) -> None:
    validate_deployment_preflight(
        profile=os.environ.get("NOMOS_DEPLOY_PROFILE"),
        secrets=[
            SecretCheck("MONITOR_INTERNAL_TOKEN", internal_token),
            SecretCheck("MONITOR_DASHBOARD_PASSWORD", dashboard_password),
            SecretCheck("MONITOR_MCP_PROXY_TOKEN", mcp_proxy_token),
        ],
    )


def _check_mcp_proxy_auth(
    authorization: str,
    *,
    expected_token: str,
) -> bool:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return False
    return hmac.compare_digest(token.strip(), expected_token)


def _bearer_auth_challenge(detail: str) -> JSONResponse:
    return JSONResponse(
        {"error": detail},
        status_code=401,
        headers={
            "WWW-Authenticate": 'Bearer realm="Nomos Monitor MCP", charset="UTF-8"',
            "Cache-Control": "no-store",
        },
    )


def build_app(
    *,
    upstream_url: str | None = None,
    upstream_health_url: str | None = None,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
    window_seconds: float = 60.0,
    history_limit: int = 5000,
) -> FastAPI:
    """Construct the monitor app.

    Tests inject `upstream_transport=httpx.ASGITransport(app=...)` to drive
    a fake upstream in-process. Production wires it to the council host.
    """
    upstream_url = upstream_url or os.environ.get("MONITOR_UPSTREAM", DEFAULT_UPSTREAM)
    upstream_health_url = upstream_health_url or os.environ.get(
        "MONITOR_UPSTREAM_HEALTH", _derive_health_url(upstream_url)
    )
    ledger_patterns = _split_env_list(os.environ.get("MONITOR_LEDGER_PATHS", "/app/ledger/*.jsonl"))
    # Trusted council public keys (hex:<64> or key file paths). With none
    # configured the dashboard checks hashes and links only and says so.
    ledger_key_specs = _split_env_list(os.environ.get("MONITOR_LEDGER_PUBLIC_KEYS", ""))
    ledger_public_keys = load_trusted_public_keys(ledger_key_specs)
    if ledger_key_specs and not ledger_public_keys:
        # The operator asked for signature verification; serving unverified
        # entries as if that were the configured mode would fail open.
        raise DeploymentPreflightError(
            [
                "MONITOR_LEDGER_PUBLIC_KEYS is set but no usable Ed25519 key loaded from "
                + ", ".join(repr(spec) for spec in ledger_key_specs)
            ]
        )
    if not ledger_public_keys:
        logger.warning("MONITOR_LEDGER_PUBLIC_KEYS not set: ledger signatures will not be verified")
    ledger_source = os.environ.get("MONITOR_LEDGER_SOURCE", "files").strip().lower()
    if ledger_source not in {"files", "api"}:
        logger.warning("invalid MONITOR_LEDGER_SOURCE=%r; using 'files'", ledger_source)
        ledger_source = "files"
    # Default the API base to the council host the proxy already targets.
    ledger_api_url = os.environ.get(
        "MONITOR_LEDGER_API_URL",
        (upstream_url or DEFAULT_UPSTREAM).rsplit("/mcp", 1)[0],
    )
    internal_token = os.environ.get("MONITOR_INTERNAL_TOKEN", "")
    dashboard_user = os.environ.get("MONITOR_DASHBOARD_USER", DEFAULT_DASHBOARD_USER)
    dashboard_password = os.environ.get("MONITOR_DASHBOARD_PASSWORD", "")
    mcp_proxy_token = os.environ.get("MONITOR_MCP_PROXY_TOKEN", "")
    _validate_monitor_deployment_preflight(
        internal_token=internal_token,
        dashboard_password=dashboard_password,
        mcp_proxy_token=mcp_proxy_token,
    )

    app = FastAPI(title="Council Monitor", docs_url=None, redoc_url=None)
    app.add_middleware(NoStoreStaticMiddleware)
    app.add_middleware(DashboardBasicAuthMiddleware)

    registry = MetricsRegistry(
        window_seconds=window_seconds,
        history_limit=history_limit,
    )
    # One long-lived AsyncClient per process keeps connection reuse to the
    # upstream council, which matters for high-rate proxying. We set this up
    # eagerly rather than in a lifespan handler because httpx ASGITransport
    # (used in tests) doesn't fire lifespan events. Process exit closes the
    # sockets — explicit shutdown isn't necessary for correctness.
    # Must exceed the council's own budget (llm_retry_budget_seconds), or the
    # proxy abandons a request the council would have answered. Council members
    # run in parallel, so this bounds the slowest member, not their sum.
    client_kwargs: dict[str, Any] = {"timeout": httpx.Timeout(180.0)}
    if upstream_transport is not None:
        client_kwargs["transport"] = upstream_transport
    app.state.metrics = registry
    # Post-gateway stats path: read the council's Redis telemetry stream instead of
    # proxy-inline recording (which is dead now the gateway owns /mcp). Opt-in via
    # MONITOR_STATS_SOURCE=redis; falls back to the in-memory registry otherwise.
    app.state.stats_reader = _build_stats_reader(window_seconds, history_limit)
    app.state.upstream = httpx.AsyncClient(**client_kwargs)
    app.state.upstream_url = upstream_url
    app.state.upstream_health_url = upstream_health_url
    app.state.ledger_patterns = ledger_patterns
    app.state.ledger_public_keys = ledger_public_keys
    app.state.ledger_source = ledger_source
    app.state.ledger_api_url = ledger_api_url
    app.state.internal_token = internal_token
    app.state.dashboard_user = dashboard_user
    app.state.dashboard_password = dashboard_password
    app.state.mcp_proxy_token = mcp_proxy_token
    logger.info("monitor proxy ready; upstream=%s", upstream_url)
    if app.state.dashboard_password:
        logger.info("monitor dashboard auth enabled; user=%s", app.state.dashboard_user)
    else:
        logger.info("monitor dashboard auth disabled; set MONITOR_DASHBOARD_PASSWORD to enable")
    if app.state.mcp_proxy_token:
        logger.info("monitor /mcp proxy auth enabled; Bearer token required")
    else:
        logger.warning(
            "monitor /mcp proxy is UNAUTHENTICATED; set MONITOR_MCP_PROXY_TOKEN "
            "to require Bearer auth on the proxy",
        )

    @app.api_route("/mcp", methods=["POST", "GET", "DELETE", "OPTIONS"])
    async def mcp_proxy(request: Request) -> Response:
        expected_token = app.state.mcp_proxy_token
        if expected_token and not _check_mcp_proxy_auth(
            request.headers.get("authorization", ""),
            expected_token=expected_token,
        ):
            return _bearer_auth_challenge("mcp proxy authentication required")
        body = await request.body()
        return await forward_mcp(
            client=app.state.upstream,
            upstream_url=app.state.upstream_url,
            method=request.method,
            request_headers=dict(request.headers),
            request_body=body,
            registry=app.state.metrics,
        )

    async def stats_registry() -> MetricsRegistry:
        """The registry the dashboard reads: the Redis replay when configured."""
        reader: RedisStatsReader | None = app.state.stats_reader
        return await reader.registry() if reader is not None else registry

    @app.get("/api/metrics")
    async def metrics() -> dict[str, Any]:
        return (await (await stats_registry()).snapshot()).to_dict()

    @app.get("/api/export.csv")
    async def export_csv() -> Response:
        snap = await (await stats_registry()).snapshot()
        return Response(
            content=_snapshot_to_csv(snap),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="council-monitor-stats.csv"'},
        )

    @app.get("/api/recent")
    async def recent(limit: int = 50) -> dict[str, Any]:
        # Cap to avoid unbounded responses if a client typos a huge limit.
        limit = max(1, min(limit, 500))
        rows = await (await stats_registry()).recent(limit=limit)
        return {"rows": [row.to_dict() for row in rows]}

    @app.get("/api/ledger")
    async def ledger(
        limit: int = 100,
        cursor: str = "",
        chain_id: str = "",
        entry_type: str = "",
        query: str = "",
        integrity: str = "",
        validate: bool = False,
    ) -> dict[str, Any]:
        if app.state.ledger_source == "api":
            return await load_ledger_page_from_api(
                app.state.upstream,
                app.state.ledger_api_url,
                limit=limit,
                cursor=cursor,
                chain_id=chain_id,
                entry_type=entry_type,
                query=query,
                integrity=integrity,
                validate=validate,
                public_keys=app.state.ledger_public_keys,
            )
        return load_ledger_page(
            patterns=app.state.ledger_patterns,
            limit=limit,
            cursor=cursor,
            chain_id=chain_id,
            entry_type=entry_type,
            query=query,
            integrity=integrity,
            validate=validate,
            public_keys=app.state.ledger_public_keys,
        )

    @app.get("/api/ledger/{chain_id}/{sequence}")
    async def ledger_entry(chain_id: str, sequence: int) -> JSONResponse:
        if app.state.ledger_source == "api":
            entry = await get_ledger_entry_from_api(
                app.state.upstream,
                app.state.ledger_api_url,
                chain_id=chain_id,
                sequence=sequence,
                public_keys=app.state.ledger_public_keys,
            )
        else:
            entry = get_ledger_entry(
                chain_id=chain_id,
                sequence=sequence,
                patterns=app.state.ledger_patterns,
                public_keys=app.state.ledger_public_keys,
            )
        if entry is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return JSONResponse(entry)

    @app.post("/internal/council-latency")
    async def council_latency(request: Request) -> JSONResponse:
        token = app.state.internal_token
        supplied_token = request.headers.get("X-Council-Internal-Token", "")
        if not token or not hmac.compare_digest(supplied_token, token):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"ok": False, "error": "payload must be object"}, status_code=400)
        await app.state.metrics.record_council_latency(payload)
        return JSONResponse({"ok": True})

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        upstream_ok = False
        upstream_detail: dict[str, Any] = {}
        try:
            client: httpx.AsyncClient = app.state.upstream
            r = await client.get(app.state.upstream_health_url, timeout=2.0)
            upstream_ok = r.status_code < 500
            upstream_detail = {"status_code": r.status_code}
        except Exception as exc:
            upstream_detail = {"error": str(exc)}
        snap = await app.state.metrics.snapshot()
        body = {
            "monitor": {"ok": True, "inflight": snap.inflight},
            "upstream": {
                "ok": upstream_ok,
                **upstream_detail,
                "url": app.state.upstream_health_url,
            },
        }
        status = 200 if upstream_ok else 503
        return JSONResponse(body, status_code=status)

    @app.get("/livez")
    async def livez() -> JSONResponse:
        # Process-only liveness/readiness for kubelet probes. The monitor is a
        # reverse proxy: it is alive and ready to serve the moment it is listening,
        # and it returns 502 per-request while the upstream council warms up. This
        # endpoint deliberately does NOT call the council, so a slow/cold or briefly
        # unavailable council never flaps readiness or (worse) gets the proxy killed
        # by liveness. Aggregate upstream health stays visible at /healthz.
        return JSONResponse({"ok": True})

    @app.get("/debug/tools/list")
    async def debug_tools_list() -> JSONResponse:
        try:
            client: httpx.AsyncClient = app.state.upstream
            response = await client.get(app.state.upstream_health_url, timeout=5.0)
            try:
                body = response.json()
            except ValueError:
                body = {"error": response.text}
            return JSONResponse(body, status_code=response.status_code)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)

    @app.get("/api/config")
    async def config() -> dict[str, Any]:
        return {
            "upstream_url": app.state.upstream_url,
            "window_seconds": registry.window_seconds,
            "ledger_patterns": list(app.state.ledger_patterns),
            "ledger_public_key_count": len(app.state.ledger_public_keys),
        }

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/")
        async def root() -> HTMLResponse:
            return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))

    return app


def _snapshot_to_csv(snap: Snapshot) -> str:
    """Flatten long-term monitor stats into a durable CSV export.

    The export intentionally uses process-lifetime counters and retained
    council latency aggregates. Rolling-window fields are dashboard-only.
    """
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["section", "name", "metric", "value"])

    for metric, value in [
        ("asof_ms", snap.asof_ms),
        ("window_seconds", snap.window_seconds),
        ("completed_total", snap.completed_total),
        ("error_total", snap.error_total),
        ("decisions_total", snap.decisions_total),
    ]:
        writer.writerow(["summary", "monitor", metric, value])
    writer.writerow(["summary", "monitor", "stats_source", snap.stats_source])

    _write_counter_rows(writer, "source_total", snap.by_source_total)
    _write_counter_rows(writer, "tool_total", snap.by_tool_total)
    _write_counter_rows(writer, "agent_total", snap.by_agent_total)
    _write_agent_tool_rows(writer, snap.by_agent_tool_total)
    _write_counter_rows(writer, "decision_outcome_total", snap.by_decision_outcome_total)
    _write_counter_rows(writer, "risk_total", snap.by_risk_total)
    _write_counter_rows(writer, "decline_reason_total", snap.by_error_class_total)
    _write_counter_rows(writer, "missing_permission_total", snap.by_missing_permission_total)
    _write_counter_rows(writer, "code_review_total", snap.by_code_review_total)
    _write_latency_rows(writer, "stage_latency", snap.council_stage_latency)
    _write_latency_rows(writer, "actor_latency", snap.council_actor_latency)
    for row in snap.llm_tokens.get("retained", {}).get("by_member", []):
        for metric in ("calls", "input", "output", "total"):
            writer.writerow(["llm_tokens", row["member_id"], metric, row[metric]])
    return out.getvalue()


def _split_env_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _write_counter_rows(writer: Any, section: str, counts: dict[str, int]) -> None:
    for name, count in sorted(counts.items()):
        writer.writerow([section, name, "count", count])


def _write_agent_tool_rows(writer: Any, agent_tools: dict[str, dict[str, int]]) -> None:
    for agent_id, counts in sorted(agent_tools.items()):
        for tool, count in sorted(counts.items()):
            writer.writerow(["agent_tool_total", agent_id, tool, count])


def _write_latency_rows(writer: Any, section: str, rows: dict[str, dict[str, Any]]) -> None:
    dimensions = {
        "role",
        "member_id",
        "provider",
        "stage",
        "tool",
        "last_decision",
    }
    for name, values in sorted(rows.items()):
        for metric, value in sorted(values.items()):
            if metric in dimensions:
                writer.writerow([section, name, metric, value])
        for metric, value in sorted(values.items()):
            if metric not in dimensions:
                writer.writerow([section, name, metric, value])


# Module-level app for `uvicorn server.main:app`.
app = build_app()
