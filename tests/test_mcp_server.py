# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import asyncio
import threading
import time

import pytest

import nomos.mcp.server as mcp_server_module
from nomos.auth.authenticator import AgentIdentity
from nomos.level_0.types import CouncilMember, Intent
from nomos.level_4.council import CouncilConfig, SecurityCouncil
from nomos.mcp.server import CouncilMCPServer


class StubAuthenticator:
    def __init__(self, identity: AgentIdentity | None):
        self._identity = identity

    async def authenticate(self, token: str) -> AgentIdentity | None:
        if not token:
            return None
        return self._identity


def _identity(agent_id: str) -> AgentIdentity:
    return AgentIdentity(agent_id=agent_id, display_name=agent_id, permissions={"shell:read"})


def _proposal_arguments() -> dict[str, str]:
    return {
        "auth_token": "token",
        "agent_id": "spoofed-agent",
        "description": "Read workspace directory for inspection",
        "target_resource": "/workspace",
        "justification": "Need to inspect files",
        "command": "/bin/ls /workspace",
    }


def test_propose_uses_authenticated_identity_not_supplied_agent_id():
    council = SecurityCouncil(config=CouncilConfig(require_justification=True))
    server = CouncilMCPServer(
        council=council,
        authenticator=StubAuthenticator(_identity("agent-a")),
    )

    result = server.call_tool(
        "propose_intent",
        _proposal_arguments(),
    )

    assert result["success"] is True
    intent_id = result["data"]["intent_id"]
    workflow = council.get_workflow(intent_id)
    assert workflow is not None
    assert workflow.intent.agent_id == "agent-a"


def test_propose_fails_closed_without_authenticator():
    council = SecurityCouncil(config=CouncilConfig(require_justification=True))
    server = CouncilMCPServer(council=council, authenticator=None)

    result = server.call_tool(
        "propose_intent",
        {
            "agent_id": "agent-a",
            "description": "Read workspace directory for inspection",
            "target_resource": "/workspace",
            "justification": "Need to inspect files",
            "command": "/bin/ls /workspace",
        },
    )

    assert result["success"] is False
    assert "Authentication service unavailable" in result["error"]


def test_approve_uses_authenticated_member_not_supplied_approver_id():
    council = SecurityCouncil(config=CouncilConfig(require_justification=True))
    council.register_council_member(CouncilMember("alice", "", "Alice"))
    council.register_council_member(CouncilMember("bob", "", "Bob"))
    intent = Intent.create(
        agent_id="requester",
        description="Run operational command with approval",
        target_resource="/workspace",
        command="/usr/local/bin/custom-admin-tool --mutate",
        justification="Need approval for test command",
    )
    submit = council.submit_intent(intent)
    assert submit.requires_approval is True

    server = CouncilMCPServer(council=council, authenticator=StubAuthenticator(_identity("alice")))
    result = server.call_tool(
        "approve_intent",
        {
            "auth_token": "token",
            "intent_id": intent.intent_id,
            "approver_id": "bob",
            "reason": "Approved by authenticated Alice",
        },
    )

    assert result["success"] is True
    workflow = council.get_workflow(intent.intent_id)
    assert workflow is not None
    assert workflow.vote_record is not None
    assert "alice" in workflow.vote_record.votes
    assert "bob" not in workflow.vote_record.votes


def test_execute_rejects_authenticated_non_owner():
    council = SecurityCouncil(config=CouncilConfig(require_justification=True))
    intent = Intent.create(
        agent_id="owner-agent",
        description="Run operational command with approval",
        target_resource="/workspace",
        command="/usr/local/bin/custom-admin-tool --mutate",
        justification="Need approval for test command",
    )
    council.submit_intent(intent)
    server = CouncilMCPServer(
        council=council,
        authenticator=StubAuthenticator(_identity("different-agent")),
    )

    result = server.call_tool(
        "execute_intent",
        {"auth_token": "token", "intent_id": intent.intent_id},
    )

    assert result["success"] is False
    assert "does not own" in result["error"]


@pytest.mark.asyncio
async def test_authentication_allows_event_loop_progress():
    class DelayedAuthenticator:
        async def authenticate(self, token: str) -> AgentIdentity | None:
            await asyncio.sleep(0.05)
            return _identity("agent-a")

    server = CouncilMCPServer(
        council=SecurityCouncil(config=CouncilConfig(require_justification=True)),
        authenticator=DelayedAuthenticator(),
    )
    loop_progressed = asyncio.Event()

    async def mark_loop_progress() -> None:
        await asyncio.sleep(0.01)
        loop_progressed.set()

    progress_task = asyncio.create_task(mark_loop_progress())
    call_task = asyncio.create_task(server.call_tool_async("propose_intent", _proposal_arguments()))

    await asyncio.wait_for(loop_progressed.wait(), timeout=1.0)
    result = await call_task
    await progress_task

    assert result["success"] is True


@pytest.mark.asyncio
async def test_call_tool_rejects_active_event_loop():
    server = CouncilMCPServer(
        council=SecurityCouncil(config=CouncilConfig(require_justification=True)),
        authenticator=StubAuthenticator(_identity("agent-a")),
    )

    with pytest.raises(
        RuntimeError,
        match="call_tool cannot run on an active event loop; await call_tool_async",
    ):
        server.call_tool("propose_intent", _proposal_arguments())


@pytest.mark.asyncio
async def test_authentication_timeout_does_not_wait_for_cleanup(monkeypatch):
    cleanup_started = asyncio.Event()

    class SlowCleanupAuthenticator:
        async def authenticate(self, token: str) -> AgentIdentity | None:
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await asyncio.sleep(0.2)

    monkeypatch.setattr(mcp_server_module, "_AUTHENTICATION_TIMEOUT_SECONDS", 0.01)
    server = CouncilMCPServer(
        council=SecurityCouncil(config=CouncilConfig(require_justification=True)),
        authenticator=SlowCleanupAuthenticator(),
    )

    started = asyncio.get_running_loop().time()
    result = await server.call_tool_async("propose_intent", _proposal_arguments())
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 1.0
    assert result["success"] is False
    assert "Authentication failed" in result["error"]
    await asyncio.wait_for(cleanup_started.wait(), timeout=1.0)
    await asyncio.sleep(0.2)


def test_authentication_timeout_without_running_event_loop(monkeypatch):
    class HangingAuthenticator:
        async def authenticate(self, token: str) -> AgentIdentity | None:
            await asyncio.Event().wait()

    monkeypatch.setattr(mcp_server_module, "_AUTHENTICATION_TIMEOUT_SECONDS", 0.01)
    server = CouncilMCPServer(
        council=SecurityCouncil(config=CouncilConfig(require_justification=True)),
        authenticator=HangingAuthenticator(),
    )

    started = time.monotonic()
    result = server.call_tool("propose_intent", _proposal_arguments())
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert result["success"] is False
    assert "Authentication failed" in result["error"]


def test_sync_authentication_timeout_does_not_wait_for_resistant_cleanup(monkeypatch):
    cleanup_finished = threading.Event()

    class CancellationResistantAuthenticator:
        async def authenticate(self, token: str) -> AgentIdentity | None:
            try:
                await asyncio.Event().wait()
            finally:
                deadline = asyncio.get_running_loop().time() + 0.2
                while (remaining := deadline - asyncio.get_running_loop().time()) > 0:
                    try:
                        await asyncio.sleep(remaining)
                    except asyncio.CancelledError:
                        continue
                cleanup_finished.set()

    monkeypatch.setattr(mcp_server_module, "_AUTHENTICATION_TIMEOUT_SECONDS", 0.01)
    server = CouncilMCPServer(
        council=SecurityCouncil(config=CouncilConfig(require_justification=True)),
        authenticator=CancellationResistantAuthenticator(),
    )

    started = time.monotonic()
    result = server.call_tool("propose_intent", _proposal_arguments())
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert result["success"] is False
    assert "Authentication failed" in result["error"]
    assert cleanup_finished.wait(timeout=0.5)


def test_sync_authentication_worker_startup_failure_does_not_hang(monkeypatch):
    def fail_before_invoke(coroutine):
        coroutine.close()
        raise RuntimeError("event loop startup failed")

    monkeypatch.setattr(mcp_server_module, "_AUTHENTICATION_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(mcp_server_module.asyncio, "run", fail_before_invoke)
    server = CouncilMCPServer(
        council=SecurityCouncil(config=CouncilConfig(require_justification=True)),
        authenticator=StubAuthenticator(_identity("agent-a")),
    )

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="event loop startup failed"):
        server.call_tool("propose_intent", _proposal_arguments())
    elapsed = time.monotonic() - started

    assert elapsed < 1.0


def test_sync_authentication_admission_bounds_stuck_workers(monkeypatch):
    release_cleanup = threading.Event()

    class StuckCleanupAuthenticator:
        async def authenticate(self, token: str) -> AgentIdentity | None:
            try:
                await asyncio.Event().wait()
            finally:
                while not release_cleanup.is_set():
                    try:
                        await asyncio.sleep(0.01)
                    except asyncio.CancelledError:
                        continue

    worker_limit = 2
    capacity = threading.BoundedSemaphore(worker_limit)
    monkeypatch.setattr(mcp_server_module, "_AUTHENTICATION_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(
        mcp_server_module,
        "_SYNCHRONOUS_AUTHENTICATION_CAPACITY",
        capacity,
    )
    server = CouncilMCPServer(
        council=SecurityCouncil(config=CouncilConfig(require_justification=True)),
        authenticator=StuckCleanupAuthenticator(),
    )
    existing_threads = set(threading.enumerate())
    workers: list[threading.Thread] = []

    try:
        for _ in range(worker_limit):
            result = server.call_tool("propose_intent", _proposal_arguments())
            assert result["success"] is False
            assert "Authentication failed" in result["error"]

        workers = [
            thread
            for thread in threading.enumerate()
            if thread not in existing_threads and thread.name == "mcp-authentication"
        ]
        assert len(workers) == worker_limit

        started = time.monotonic()
        rejected = [server.call_tool("propose_intent", _proposal_arguments()) for _ in range(20)]
        elapsed = time.monotonic() - started

        assert elapsed < 1.0
        assert all(result["success"] is False for result in rejected)
        assert all("capacity exhausted" in result["error"] for result in rejected)
        assert (
            len(
                [
                    thread
                    for thread in threading.enumerate()
                    if thread not in existing_threads and thread.name == "mcp-authentication"
                ]
            )
            == worker_limit
        )
    finally:
        release_cleanup.set()
        for worker in workers:
            worker.join(timeout=0.5)

    assert all(not worker.is_alive() for worker in workers)
    for _ in range(worker_limit):
        assert capacity.acquire(blocking=False)
    for _ in range(worker_limit):
        capacity.release()


@pytest.mark.asyncio
async def test_failed_authentication_is_returned_to_caller():
    server = CouncilMCPServer(
        council=SecurityCouncil(config=CouncilConfig(require_justification=True)),
        authenticator=StubAuthenticator(None),
    )

    result = await server.call_tool_async("propose_intent", _proposal_arguments())

    assert result["success"] is False
    assert "Authentication failed" in result["error"]
