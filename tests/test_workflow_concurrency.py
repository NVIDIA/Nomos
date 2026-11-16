# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""The council sizes NAT's per-tool workflow concurrency from the environment."""

from __future__ import annotations

import pytest

pytest.importorskip("nat.plugins.mcp.server.front_end_plugin_worker")

from nomos.nat import stateless_worker as sw


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 8), ("", 8), ("32", 32), (" 16 ", 16), ("0", 0), ("-4", 0), ("lots", 8)],
)
def test_workflow_max_concurrency_reads_the_environment(monkeypatch, raw, expected) -> None:
    if raw is None:
        monkeypatch.delenv(sw.WORKFLOW_MAX_CONCURRENCY_ENV, raising=False)
    else:
        monkeypatch.setenv(sw.WORKFLOW_MAX_CONCURRENCY_ENV, raw)
    assert sw.workflow_max_concurrency() == expected


@pytest.mark.asyncio
async def test_sized_session_manager_passes_the_configured_limit(monkeypatch) -> None:
    seen: dict[str, object] = {}

    async def fake_create(cls, *args, **kwargs):
        seen.update(kwargs)
        return "manager"

    monkeypatch.setattr(sw.SessionManager, "create", classmethod(fake_create))
    monkeypatch.setenv(sw.WORKFLOW_MAX_CONCURRENCY_ENV, "24")

    assert await sw._SizedSessionManager.create(config=None, shared_builder=None) == "manager"
    assert seen["max_concurrency"] == 24

    # An explicit caller value is left alone.
    await sw._SizedSessionManager.create(config=None, shared_builder=None, max_concurrency=3)
    assert seen["max_concurrency"] == 3


def test_credentials_are_redacted_from_nat_error_logs(caplog) -> None:
    import logging

    sw.install_credential_redaction()
    sw.install_credential_redaction()  # idempotent
    nat_logger = logging.getLogger("nat.builder.function")
    assert sum(isinstance(f, sw.RedactCredentials) for f in nat_logger.filters) == 1

    with caplog.at_level(logging.ERROR, logger="nat.builder.function"):
        nat_logger.error(
            "Error with ainvoke in function with input: %s",
            "api_version='v1' auth_token='not-a-real-token' agent_id='agent-developer'",
        )
    text = caplog.records[-1].getMessage()
    assert "not-a-real-token" not in text
    assert "auth_token='[redacted]'" in text
    assert "agent_id='agent-developer'" in text


def _nat_error_text(caplog, *args: object) -> str:
    import logging

    sw.install_credential_redaction()
    nat_logger = logging.getLogger("nat.builder.function")
    with caplog.at_level(logging.ERROR, logger="nat.builder.function"):
        # The statement NAT runs when a tool call raises.
        nat_logger.error("Error with ainvoke in function with input: %s. Error: %s", *args)
    return caplog.records[-1].getMessage()


def _failed_authorize_input() -> dict[str, object]:
    return {
        "auth_token": "CANARY-TOKEN",
        "agent_id": "agent-developer",
        "command": "deploy --region us-west",
        "env": {
            "API_KEY": "CANARY-API",  # pragma: allowlist secret
            "GITHUB_TOKEN": "CANARY-GH",
            "REGION": "CANARY-PLAIN",
        },
        "tool_call": {
            "arguments": {"db_password": "CANARY-DB", "path": "/srv"},  # pragma: allowlist secret
        },
    }


@pytest.mark.parametrize("as_model", [False, True])
def test_a_failed_authorize_logs_no_credential_or_environment_value(caplog, as_model) -> None:
    """NAT passes the tool input itself to the logger, as a mapping or a model."""
    from pydantic import BaseModel

    class Input(BaseModel):
        auth_token: str
        agent_id: str
        command: str
        env: dict[str, str]
        tool_call: dict[str, dict[str, str]]

    value = _failed_authorize_input()
    text = _nat_error_text(caplog, Input(**value) if as_model else value, RuntimeError("boom"))

    assert "CANARY" not in text
    # What identifies the failed call stays readable.
    assert "agent-developer" in text and "deploy --region us-west" in text
    assert "/srv" in text and "boom" in text


@pytest.mark.parametrize(
    "logged",
    [
        # pragma: allowlist nextline secret
        "auth_token='CANARY' agent_id='agent-developer' env={'API_KEY': 'CANARY', 'H': 'CANARY'}",
        "{'auth_token': 'CANARY', 'agent_id': 'agent-developer', 'env': {'A_TOKEN': 'CANARY'}}",
        '{"agent_id": "agent-developer", "password": "CANARY", "env": {"X": "CANARY"}}',
        "agent_id='agent-developer' MY_API_KEY=CANARY client_secret: CANARY",
        "agent_id='agent-developer' env={'BRACE': 'CANARY}CANARY', 'HOME': 'CANARY'}",
    ],
)
def test_already_formatted_input_is_redacted_by_pattern(caplog, logged) -> None:
    text = _nat_error_text(caplog, logged, RuntimeError("boom"))

    assert "CANARY" not in text
    assert "agent-developer" in text and "boom" in text


def test_credentials_under_other_names_and_shapes_are_masked(caplog) -> None:
    value = {
        "agent_id": "agent-developer",
        "tool_call": {
            "arguments": {
                "headers": {
                    "Authorization": "Bearer CANARY-AUTH",
                    "Cookie": "sid=CANARY-COOKIE",
                    "X-Api-Key": "CANARY-XKEY",
                },
                "private_key": "CANARY-PEM",  # pragma: allowlist secret
                "passphrase": "CANARY-PHRASE",
                "passwd": "CANARY-PASSWD",  # pragma: allowlist secret
                "environment": {"PATH": "CANARY-ENVIRONMENT"},
                "variables": [{"name": "API_KEY", "value": "CANARY-PAIR"}],
                "url": "https://deploy:CANARY-URL@registry.invalid/v2/",  # pragma: allowlist secret
                "path": "/srv",
            }
        },
    }
    text = _nat_error_text(caplog, value, RuntimeError("boom"))

    assert "CANARY" not in text
    assert "agent-developer" in text and "/srv" in text and "registry.invalid" in text


@pytest.mark.parametrize(
    "logged",
    [
        "agent_id='agent-developer' {'tokens': ['CANARY', 'CANARY']}",
        "agent_id='agent-developer' {'secrets': {'a': 'CANARY', 'b': 'CANARY'}}",
        "agent_id='agent-developer' X-Api-Key: CANARY",
        "agent_id='agent-developer' environment={'A': 'CANARY'}",
    ],
)
def test_bracketed_and_hyphenated_text_forms_are_masked(caplog, logged) -> None:
    text = _nat_error_text(caplog, logged, RuntimeError("boom"))

    assert "CANARY" not in text
    assert "agent-developer" in text and "boom" in text


def test_redaction_time_stays_linear_on_hostile_text(caplog) -> None:
    """A long unbroken run of credential words must not stall the event loop.

    An unbounded name pattern took about 20 s on 80 KB of this input.
    """
    import time

    hostile = {"description": "token" * 80_000, "agent_id": "agent-developer"}  # 400 KB
    started = time.perf_counter()
    text = _nat_error_text(caplog, hostile, RuntimeError("boom"))

    assert time.perf_counter() - started < 5
    assert "agent-developer" in text


def test_a_failing_redaction_withholds_the_record_and_never_raises(caplog) -> None:
    cyclic: dict[str, object] = {"auth_token": "CANARY"}
    cyclic["self"] = cyclic

    text = _nat_error_text(caplog, cyclic, RuntimeError("boom"))

    assert "CANARY" not in text
    assert "redaction failed" in text
