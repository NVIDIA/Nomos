# SPDX-License-Identifier: Apache-2.0
"""Local-only synthetic source using the real Nomos PostgreSQL ledger backend."""

from __future__ import annotations

import hmac
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from assurance.fixtures import LIVE_SCENARIOS, ScenarioName, live_scenario, scenarios
from nomos.level_0.crypto import KeyPair, Signer
from nomos.level_1.ledger.chain import LedgerChain
from nomos.level_1.ledger.entry import LedgerEntryType


class PageRequest(BaseModel):
    council_id: str
    cursor: int | None = Field(default=None, ge=0)
    limit: int = Field(default=100, ge=1, le=200)


class EntryRequest(BaseModel):
    council_id: str
    sequence: int = Field(ge=0)


class FaultRequest(BaseModel):
    enabled: bool


class LiveScenarioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario: ScenarioName
    run_id: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9][a-z0-9-]*$")


def create_app(
    *,
    chain: LedgerChain | None = None,
    token: str | None = None,
    fixture_token: str | None = None,
    council_id: str | None = None,
) -> FastAPI:
    if chain is None and os.environ.get("DEMO_MODE") != "1":
        raise ValueError("synthetic source requires explicit DEMO_MODE=1")
    token = token or os.environ["SOURCE_TOKEN"]
    fixture_token = fixture_token or os.environ["FIXTURE_TOKEN"]
    council_id = council_id or os.environ["COUNCIL_ID"]
    if not token or not fixture_token:
        raise ValueError("SOURCE_TOKEN must not be empty")
    lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app):
        pool = None
        if chain is None:
            from psycopg_pool import ConnectionPool

            from nomos.level_1.ledger.postgres_storage import PostgresLedgerStorage
            from nomos.store.migrate import run_migrations

            dsn = os.environ["DATABASE_URL"]
            run_migrations(dsn)
            pool = ConnectionPool(dsn, min_size=1, max_size=4)
            pool.wait(timeout=30)
            key = KeyPair.from_private_key_bytes(
                Path(os.environ["SIGNING_KEY_FILE"]).read_bytes(), "assurance-demo-signer"
            )
            chain_id = os.environ["CHAIN_ID"]
            app.state.chain = LedgerChain(
                storage=PostgresLedgerStorage(pool, chain_id, council_id=council_id),
                signer=Signer(key),
                chain_id=chain_id,
            )
            app.state.chain.initialize(
                "assurance-demo", "Synthetic assurance fixtures; no real execution"
            )
        yield
        if pool:
            pool.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.chain = chain
    app.state.fault = False
    auth = HTTPBearer(auto_error=False)

    def authenticate(credentials: HTTPAuthorizationCredentials | None = Depends(auth)):
        if credentials is None or not hmac.compare_digest(
            credentials.credentials.encode(), token.encode()
        ):
            raise HTTPException(401, "Authentication required")

    def fixture_auth(credentials: HTTPAuthorizationCredentials | None = Depends(auth)):
        if credentials is None or not hmac.compare_digest(
            credentials.credentials.encode(), fixture_token.encode()
        ):
            raise HTTPException(401, "Fixture operator authentication required")

    def entry(sequence: int):
        item = app.state.chain.get(sequence)
        if item is None:
            return None
        row = item.to_dict()
        # to_dict retains payload references with in-memory storage: copy before fault injection.
        row["payload"] = dict(row["payload"])
        if app.state.fault and sequence == app.state.chain.get_head().sequence:
            row["payload"]["synthetic_response_corruption"] = True
        return row

    def require_council(requested: str):
        if requested != council_id:
            raise HTTPException(404, "Unknown council")

    @app.get("/healthz")
    def health():
        return {"status": "up", "synthetic": True}

    @app.post("/debug/tools/call/council_ledger_page", dependencies=[Depends(authenticate)])
    def page(request: PageRequest):
        require_council(request.council_id)
        ledger = app.state.chain
        head = ledger.get_head().sequence
        end = head if request.cursor is None else min(head, request.cursor)
        low = max(0, end - request.limit + 1)
        return {
            "council_id": council_id,
            "chain_id": ledger.chain_id,
            "head_sequence": head,
            "entries": [entry(i) for i in range(end, low - 1, -1)],
            "next_cursor": low - 1 if low > 0 else None,
        }

    @app.post("/debug/tools/call/council_ledger_get", dependencies=[Depends(authenticate)])
    def get(request: EntryRequest):
        require_council(request.council_id)
        row = entry(request.sequence)
        return {"chain_id": app.state.chain.chain_id, "found": row is not None, "entry": row}

    @app.post("/fixtures/seed", dependencies=[Depends(fixture_auth)])
    def seed():
        with lock:
            ledger = app.state.chain
            existing = {ledger.get(i).payload.get("fixture_id") for i in range(ledger.length())}
            appended = 0
            for kind, payload in scenarios():
                if payload["fixture_id"] not in existing:
                    ledger.append(LedgerEntryType(kind), payload)
                    appended += 1
        return {
            "appended": appended,
            "synthetic": True,
            "head_sequence": ledger.get_head().sequence,
        }

    @app.post("/fixtures/run", dependencies=[Depends(fixture_auth)])
    def run_scenario(request: LiveScenarioRequest):
        events = live_scenario(request.scenario, request.run_id, council_id)
        with lock:
            ledger = app.state.chain
            existing = {ledger.get(i).payload.get("fixture_id"): i for i in range(ledger.length())}
            appended, sequences = 0, []
            for kind, payload in events:
                sequence = existing.get(payload["fixture_id"])
                if sequence is None:
                    sequence = ledger.append(LedgerEntryType(kind), payload).sequence
                    appended += 1
                sequences.append(sequence)
            head = ledger.get_head().sequence
        return {
            "scenario": request.scenario,
            "run_id": request.run_id,
            "agent_id": events[0][1]["agent_id"],
            "synthetic": True,
            "appended": appended,
            "sequences": sequences,
            "head_sequence": head,
            "expected_findings": LIVE_SCENARIOS[request.scenario]["expected"],
        }

    @app.post("/fixtures/fault", dependencies=[Depends(fixture_auth)])
    def fault(request: FaultRequest):
        app.state.fault = request.enabled
        return {"response_corruption": app.state.fault, "ledger_modified": False}

    return app
