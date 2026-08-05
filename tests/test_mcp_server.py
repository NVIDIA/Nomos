# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

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


def test_propose_uses_authenticated_identity_not_supplied_agent_id():
    council = SecurityCouncil(config=CouncilConfig(require_justification=True))
    server = CouncilMCPServer(
        council=council,
        authenticator=StubAuthenticator(_identity("agent-a")),
    )

    result = server.call_tool(
        "propose_intent",
        {
            "auth_token": "token",
            "agent_id": "spoofed-agent",
            "description": "Read workspace directory for inspection",
            "target_resource": "/workspace",
            "justification": "Need to inspect files",
            "command": "/bin/ls /workspace",
        },
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
