# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Durable, independent analysis streams over immutable verified evidence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from assurance.monitor import Store

Analyzer = Callable[[list[dict], str], Awaitable[tuple[list[dict], dict]]]
FINISHED = {"complete", "insufficient_evidence", "not_configured", "skipped"}


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def public_run(run: dict) -> dict:
    """Raw evidence and document contents remain in the protected local store."""
    return {
        key: value for key, value in run.items() if key not in {"input_snapshot", "ethos_artifacts"}
    }


class StreamWorker:
    def __init__(
        self,
        store: Store,
        stream: str,
        analyzer: Analyzer,
        *,
        config: dict,
        artifacts: dict | None = None,
        interval: float = 300,
        timeout: float = 120,
        max_attempts: int = 3,
        window_size: int = 2000,
    ):
        self.store, self.stream, self.analyzer = store, stream, analyzer
        self.config, self.config_digest = config, digest(config)
        self.artifacts = artifacts or {}
        self.interval, self.timeout, self.max_attempts = interval, timeout, max_attempts
        self.window_size = window_size

    def progress(self) -> int:
        return self.store.progress(self.stream, self.config_digest)

    def status(self) -> dict:
        run = self.store.stream_run(self.stream, self.config_digest)
        return {
            "enabled": True,
            "sequence": self.progress(),
            "config_digest": self.config_digest,
            "latest_run": public_run(run) if run else None,
            "next_due": self.store.setting(self._due_key, 0),
        }

    @property
    def _due_key(self):
        return f"next_due:{self.stream}:{self.config_digest}"

    async def step(self, now: float | None = None) -> dict | None:
        now = time.time() if now is None else now
        run = self.store.stream_run(self.stream, self.config_digest)
        if run and run["status"] not in FINISHED:
            if run["attempts"] >= self.max_attempts:
                if run["status"] != "failed":
                    run.update(
                        status="failed",
                        error_type="InterruptedRun",
                        completed_at=datetime.now(UTC).isoformat(),
                        next_attempt=0,
                        coverage={
                            "model_requests": None,
                            "usage_status": "unavailable_after_restart",
                        },
                    )
                    self.store.save_run(run)
                return public_run(run)
            if run.get("next_attempt", 0) > now:
                return public_run(run)
        else:
            if now < self.store.setting(self._due_key, 0):
                return None
            run = self.store.make_run(
                self.stream,
                self.config_digest,
                artifacts=self.artifacts,
                window_size=self.window_size,
            )
            if run is None:
                return None
        # Metadata is restored even if a process stopped just after make_run committed.
        run.setdefault("configuration", self.config)
        run.setdefault(
            "agents",
            sorted(
                {
                    e["payload"].get("agent_id", "unattributed")
                    for e in run["input_snapshot"]
                    if isinstance(e["payload"].get("agent_id", "unattributed"), str)
                }
            ),
        )
        run.update(
            status="running",
            attempts=run["attempts"] + 1,
            started_at=datetime.now(UTC).isoformat(),
            completed_at=None,
            coverage={},
            error_type=None,
        )
        self.store.save_run(run)
        try:
            findings, coverage = await asyncio.wait_for(
                self.analyzer(run["input_snapshot"], self.store.chain_id), self.timeout
            )
            coverage["evidence_window"] = {
                "sequence_start": run["evidence_start"],
                "sequence_end": run["sequence_end"],
                "event_count": len(run["input_snapshot"]),
                "event_limit": self.window_size,
                "earlier_context_omitted": run["evidence_start"] > 0,
            }
            if run["evidence_start"] > 0:
                coverage.setdefault("limitations", []).append(
                    "Earlier ledger context is outside this bounded window; "
                    "long-running tasks may be incomplete."
                )
            state = coverage.get("status", "complete")
            if state not in FINISHED:
                raise ValueError("analyzer returned an invalid completion status")
            run.update(
                status=state,
                coverage=coverage,
                completed_at=datetime.now(UTC).isoformat(),
                next_attempt=0,
            )
            self.store.save_run(run, findings)
            caught_up = run["sequence_end"] >= self.store.checkpoint()[0]
            self.store.set_setting(self._due_key, now + self.interval if caught_up else 0)
        except asyncio.CancelledError:
            # Preserve the immutable snapshot; a new process resumes it.
            run.update(status="pending", next_attempt=0)
            self.store.save_run(run)
            raise
        except Exception as exc:
            # Upstream messages can include prompts, credentials, or source payloads.
            run.update(
                status="failed",
                error_type=type(exc).__name__,
                coverage=(
                    {"model_requests": None, "usage_status": "unavailable_after_timeout"}
                    if isinstance(exc, TimeoutError)
                    else getattr(exc, "coverage", {})
                ),
                completed_at=datetime.now(UTC).isoformat(),
                next_attempt=now + min(60, 2 ** run["attempts"]),
            )
            self.store.save_run(run)
        return public_run(run)

    def retry(self, run: dict) -> None:
        if run["config_digest"] != self.config_digest or run["stream"] != self.stream:
            raise ValueError("run configuration differs from the active configuration")
        if run["status"] != "failed":
            raise ValueError("only failed runs can be retried")
        run.update(status="pending", attempts=0, next_attempt=0, completed_at=None)
        self.store.save_run(run)
