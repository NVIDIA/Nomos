# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import io
import json
import logging
import sys

from nomos.logging import (
    StructuredFormatter,
    audit_log,
    configure_structured_logging,
)


def test_structured_formatter_emits_json_with_standard_and_extra_fields():
    record = logging.LogRecord(
        name="nomos.test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=10,
        msg="permission check %s",
        args=("failed",),
        exc_info=None,
    )
    record.correlation_id = "intent-123"
    record.agent_id = "agent-a"
    record.action = "permission.check"
    record.intent_id = "intent-123"

    payload = json.loads(StructuredFormatter().format(record))

    assert payload["level"] == "WARNING"
    assert payload["logger"] == "nomos.test"
    assert payload["message"] == "permission check failed"
    assert payload["correlation_id"] == "intent-123"
    assert payload["agent_id"] == "agent-a"
    assert payload["action"] == "permission.check"
    assert payload["intent_id"] == "intent-123"
    assert "timestamp" in payload


def test_structured_formatter_includes_exception_message():
    try:
        raise ValueError("bad council response")
    except ValueError:
        record = logging.getLogger("nomos.test").makeRecord(
            "nomos.test",
            logging.ERROR,
            __file__,
            30,
            "council failed",
            args=(),
            exc_info=sys.exc_info(),
        )

    payload = json.loads(StructuredFormatter().format(record))

    assert payload["message"] == "council failed"
    assert payload["exception"] == "bad council response"


def test_configure_structured_logging_replaces_root_handlers_and_level():
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    old_level = root.level
    root.addHandler(logging.NullHandler())

    try:
        configure_structured_logging("debug")

        assert root.level == logging.DEBUG
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, StructuredFormatter)
    finally:
        root.handlers.clear()
        root.handlers.extend(old_handlers)
        root.setLevel(old_level)


def test_configure_structured_logging_defaults_unknown_level_to_info():
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    old_level = root.level

    try:
        configure_structured_logging("not-a-level")

        assert root.level == logging.INFO
    finally:
        root.handlers.clear()
        root.handlers.extend(old_handlers)
        root.setLevel(old_level)


def test_audit_log_builds_structured_message_and_correlation_fields():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(StructuredFormatter())
    logger = logging.getLogger("nomos.audit.test")
    old_handlers = list(logger.handlers)
    old_level = logger.level
    old_propagate = logger.propagate
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    try:
        audit_log(
            logger,
            "writ.minted",
            agent_id="agent-a",
            intent_id="intent-123",
            writ_id="writ-456",
            detail="approved by council",
            level=logging.WARNING,
        )
    finally:
        logger.handlers.clear()
        logger.handlers.extend(old_handlers)
        logger.setLevel(old_level)
        logger.propagate = old_propagate

    payload = json.loads(stream.getvalue())
    assert payload["level"] == "WARNING"
    assert payload["message"] == "[writ.minted] approved by council"
    assert payload["action"] == "writ.minted"
    assert payload["agent_id"] == "agent-a"
    assert payload["intent_id"] == "intent-123"
    assert payload["writ_id"] == "writ-456"
    assert payload["correlation_id"] == "writ-456"


def test_audit_log_uses_intent_id_as_correlation_when_writ_id_absent():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(StructuredFormatter())
    logger = logging.getLogger("nomos.audit.intent")
    old_handlers = list(logger.handlers)
    old_level = logger.level
    old_propagate = logger.propagate
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    try:
        audit_log(logger, "intent.submitted", intent_id="intent-123")
    finally:
        logger.handlers.clear()
        logger.handlers.extend(old_handlers)
        logger.setLevel(old_level)
        logger.propagate = old_propagate

    payload = json.loads(stream.getvalue())
    assert payload["message"] == "[intent.submitted]"
    assert payload["correlation_id"] == "intent-123"
