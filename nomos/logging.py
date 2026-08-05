# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Structured JSON logging for Nomos.

All log entries include timestamp, correlation ID (intent_id or writ_id),
agent_id, and action. Never logs raw auth tokens, full command output
at INFO level, or environment variable values.
"""

import json
import logging
from typing import Any


class StructuredFormatter(logging.Formatter):
    """JSON log formatter — one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Add structured fields if present
        for key in ("correlation_id", "agent_id", "action", "writ_id", "intent_id"):
            val = getattr(record, key, None)
            if val is not None:
                entry[key] = val

        if record.exc_info and record.exc_info[1]:
            entry["exception"] = str(record.exc_info[1])

        return json.dumps(entry, default=str)


def configure_structured_logging(level: str = "INFO") -> None:
    """Configure root logger with structured JSON output."""
    handler = logging.StreamHandler()
    handler.setFormatter(StructuredFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def audit_log(
    logger: logging.Logger,
    action: str,
    *,
    agent_id: str = "",
    intent_id: str = "",
    writ_id: str = "",
    detail: str = "",
    level: int = logging.INFO,
    **extra: Any,
) -> None:
    """
    Emit a structured audit log entry.

    Use this for security-relevant events: auth attempts, permission checks,
    chairman decisions, member votes, writ minting, execution results.
    """
    msg = f"[{action}]"
    if detail:
        msg += f" {detail}"

    record_extra = {
        "action": action,
        "agent_id": agent_id,
        "intent_id": intent_id,
        "writ_id": writ_id,
        "correlation_id": writ_id or intent_id,
    }
    record_extra.update(extra)

    logger.log(level, msg, extra=record_extra)
