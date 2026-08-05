# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
MCP Server implementation for the Security Council.

Exposes council functionality as MCP tools that can be consumed by any agent.
All state-mutating tools require a valid ``auth_token`` and fail closed when no
``AgentAuthenticator`` is configured. Read-only tools remain unauthenticated.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from nomos.auth.authenticator import AgentAuthenticator
from nomos.level_0.types import CouncilMember
from nomos.level_4.council import CouncilConfig, SecurityCouncil

from .tools import (
    ToolResult,
    approve_intent,
    check_proposal_status,
    deny_intent,
    execute_intent,
    get_ledger_status,
    get_pending_approvals,
    propose_intent,
)

logger = logging.getLogger(__name__)

# Tools that mutate state and therefore require authentication.
_AUTHENTICATED_TOOLS = frozenset(
    {
        "propose_intent",
        "approve_intent",
        "deny_intent",
        "execute_intent",
    }
)


class MCPToolDefinition:
    """Definition of an MCP tool."""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: Callable[..., ToolResult],
    ):
        self.name = name
        self.description = description
        self.parameters = parameters
        self.handler = handler

    def to_schema(self) -> dict:
        """Return MCP-compatible tool schema."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": {
                "type": "object",
                "properties": self.parameters,
                "required": [k for k, v in self.parameters.items() if v.get("required", False)],
            },
        }


class CouncilMCPServer:
    """
    MCP Server that exposes Security Council as tools.

    This is a thin facade that translates MCP tool calls to council operations.
    Can be served via stdio, HTTP/SSE, or other MCP transports.

    When an ``AgentAuthenticator`` is provided, state-mutating tools
    (propose, approve, deny, execute) require a valid ``auth_token``
    parameter.  Read-only tools (check_proposal_status, get_ledger_status,
    get_pending_approvals) are always accessible.
    """

    def __init__(
        self,
        council: SecurityCouncil | None = None,
        config: CouncilConfig | None = None,
        authenticator: AgentAuthenticator | None = None,
    ):
        self._council = council or SecurityCouncil(config=config)
        self._authenticator = authenticator
        self._tools: dict[str, MCPToolDefinition] = {}
        self._register_default_tools()

    @property
    def council(self) -> SecurityCouncil:
        return self._council

    def _register_default_tools(self) -> None:
        """Register the default council tools."""

        # ── auth_token parameter (added to all mutating tools) ────────
        _auth_token_param = {
            "type": "string",
            "description": "Agent authentication token (required when auth is enabled)",
        }

        self._tools["propose_intent"] = MCPToolDefinition(
            name="propose_intent",
            description=(
                "Propose an intent for a privileged action."
                " The council will evaluate and potentially require approval."
            ),
            parameters={
                "auth_token": _auth_token_param,
                "agent_id": {
                    "type": "string",
                    "description": "ID of the requesting agent",
                    "required": True,
                },
                "command": {
                    "type": "string",
                    "description": "Exact command to evaluate",
                },
                "action_type": {
                    "type": "string",
                    "description": "Legacy action type override for non-command callers",
                },
                "description": {
                    "type": "string",
                    "description": "What you want to do",
                    "required": True,
                },
                "target_resource": {
                    "type": "string",
                    "description": "The resource to act upon",
                    "required": True,
                },
                "justification": {
                    "type": "string",
                    "description": "Why this action is needed",
                    "required": True,
                },
                "working_directory": {
                    "type": "string",
                    "description": "Directory where the command runs",
                },
                "target_host": {
                    "type": "string",
                    "description": "Target host machine",
                },
                "target_environment": {
                    "type": "string",
                    "description": "development, staging, production, or dr",
                },
                "target_cluster": {
                    "type": "string",
                    "description": "Kubernetes cluster name",
                },
                "target_namespace": {
                    "type": "string",
                    "description": "Kubernetes namespace",
                },
                "parameters": {"type": "object", "description": "Action-specific parameters"},
                "blast_radius": {
                    "type": "string",
                    "description": "Description of potential impact",
                },
                "evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Supporting evidence",
                },
                "context": {
                    "type": "string",
                    "description": "What the agent was doing before this request",
                },
                "rollback_strategy": {
                    "type": "string",
                    "enum": ["none", "automatic", "manual"],
                    "description": "Rollback approach",
                },
                "rollback_steps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Steps for automatic rollback",
                },
            },
            handler=self._handle_propose_intent,
        )

        self._tools["check_proposal_status"] = MCPToolDefinition(
            name="check_proposal_status",
            description="Check the status of a previously submitted intent.",
            parameters={
                "intent_id": {
                    "type": "string",
                    "description": "ID of the intent to check",
                    "required": True,
                },
            },
            handler=self._handle_check_status,
        )

        self._tools["approve_intent"] = MCPToolDefinition(
            name="approve_intent",
            description=(
                "Approve an intent that is awaiting approval. Requires council member privileges."
            ),
            parameters={
                "auth_token": _auth_token_param,
                "intent_id": {
                    "type": "string",
                    "description": "ID of the intent to approve",
                    "required": True,
                },
                "approver_id": {
                    "type": "string",
                    "description": "ID of the approving council member",
                    "required": True,
                },
                "reason": {"type": "string", "description": "Reason for approval"},
            },
            handler=self._handle_approve,
        )

        self._tools["deny_intent"] = MCPToolDefinition(
            name="deny_intent",
            description=(
                "Deny an intent that is awaiting approval. Requires council member privileges."
            ),
            parameters={
                "auth_token": _auth_token_param,
                "intent_id": {
                    "type": "string",
                    "description": "ID of the intent to deny",
                    "required": True,
                },
                "denier_id": {
                    "type": "string",
                    "description": "ID of the denying council member",
                    "required": True,
                },
                "reason": {"type": "string", "description": "Reason for denial", "required": True},
            },
            handler=self._handle_deny,
        )

        self._tools["execute_intent"] = MCPToolDefinition(
            name="execute_intent",
            description=(
                "Execute an approved intent."
                " Mints capability token, executes action, and verifies result."
            ),
            parameters={
                "auth_token": _auth_token_param,
                "intent_id": {
                    "type": "string",
                    "description": "ID of the intent to execute",
                    "required": True,
                },
            },
            handler=self._handle_execute,
        )

        self._tools["get_ledger_status"] = MCPToolDefinition(
            name="get_ledger_status",
            description="Get the current status of the permissioned action ledger for audit.",
            parameters={},
            handler=self._handle_ledger_status,
        )

        self._tools["get_pending_approvals"] = MCPToolDefinition(
            name="get_pending_approvals",
            description="List all intents currently awaiting approval.",
            parameters={},
            handler=self._handle_pending_approvals,
        )

    # ── Authentication ────────────────────────────────────────────────

    async def _authenticate(self, auth_token: str) -> str | None:
        """Validate auth_token and return the agent_id, or None on failure."""
        if self._authenticator is None:
            return None
        identity = await self._authenticator.authenticate(auth_token)
        if identity is None:
            return None
        return identity.agent_id

    def _bind_authenticated_identity(
        self,
        name: str,
        arguments: dict[str, Any],
        authenticated_agent_id: str,
    ) -> dict[str, Any]:
        """Make the validated token identity authoritative for mutating calls."""
        bound = dict(arguments)
        if name == "propose_intent":
            bound["agent_id"] = authenticated_agent_id
        elif name == "approve_intent":
            bound["approver_id"] = authenticated_agent_id
        elif name == "deny_intent":
            bound["denier_id"] = authenticated_agent_id
        elif name == "execute_intent":
            intent_id = str(bound.get("intent_id", ""))
            workflow = self._council.get_workflow(intent_id)
            if workflow is None:
                raise PermissionError(f"Intent {intent_id} not found")
            if workflow.intent.agent_id != authenticated_agent_id:
                raise PermissionError("Authenticated agent does not own this intent")
        return bound

    # ── Tool handlers ─────────────────────────────────────────────────

    def _handle_propose_intent(self, auth_token: str = "", **kwargs) -> ToolResult:
        return propose_intent(self._council, **kwargs)

    def _handle_check_status(self, intent_id: str) -> ToolResult:
        return check_proposal_status(self._council, intent_id)

    def _handle_approve(
        self, auth_token: str = "", intent_id: str = "", approver_id: str = "", reason: str = ""
    ) -> ToolResult:
        return approve_intent(self._council, intent_id, approver_id, reason)

    def _handle_deny(
        self, auth_token: str = "", intent_id: str = "", denier_id: str = "", reason: str = ""
    ) -> ToolResult:
        return deny_intent(self._council, intent_id, denier_id, reason)

    def _handle_execute(self, auth_token: str = "", intent_id: str = "") -> ToolResult:
        return execute_intent(self._council, intent_id)

    def _handle_ledger_status(self) -> ToolResult:
        return get_ledger_status(self._council)

    def _handle_pending_approvals(self) -> ToolResult:
        return get_pending_approvals(self._council)

    def list_tools(self) -> list[dict]:
        """Return list of available tools in MCP schema format."""
        return [tool.to_schema() for tool in self._tools.values()]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """
        Call a tool by name with the given arguments.

        This is the main entry point for MCP tool invocations.
        State-mutating tools are always authenticated. Without an authenticator,
        these tools fail closed.
        """
        tool = self._tools.get(name)
        if tool is None:
            return {
                "success": False,
                "error": f"Unknown tool: {name}",
                "data": {},
            }

        # Authenticate state-mutating tools
        if name in _AUTHENTICATED_TOOLS:
            if self._authenticator is None:
                return {
                    "success": False,
                    "error": "Authentication service unavailable",
                    "data": {},
                }
            auth_token = arguments.get("auth_token", "")
            if not auth_token:
                return {
                    "success": False,
                    "error": "auth_token is required for this tool",
                    "data": {},
                }
            # Run async authenticate in sync context
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    import concurrent.futures

                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        agent_id = pool.submit(asyncio.run, self._authenticate(auth_token)).result(
                            timeout=10
                        )
                else:
                    agent_id = loop.run_until_complete(self._authenticate(auth_token))
            except Exception:
                agent_id = None

            if agent_id is None:
                logger.warning("MCP auth failed for tool %s", name)
                return {
                    "success": False,
                    "error": "Authentication failed: invalid or expired token",
                    "data": {},
                }
            try:
                arguments = self._bind_authenticated_identity(name, arguments, agent_id)
            except PermissionError as e:
                return {
                    "success": False,
                    "error": str(e),
                    "data": {},
                }

        try:
            result = tool.handler(**arguments)
            return result.to_dict()
        except TypeError as e:
            return {
                "success": False,
                "error": f"Invalid arguments: {e}",
                "data": {},
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Tool execution failed: {e}",
                "data": {},
            }

    def register_council_member(self, member: CouncilMember) -> None:
        """Register a council member for approval workflows."""
        self._council.register_council_member(member)

    def handle_mcp_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """
        Handle a raw MCP JSON-RPC request.

        Supports:
        - tools/list: List available tools
        - tools/call: Invoke a tool
        """
        method = request.get("method", "")
        params = request.get("params", {})
        request_id = request.get("id")

        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"tools": self.list_tools()},
            }

        if method == "tools/call":
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})
            result = self.call_tool(tool_name, arguments)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": json.dumps(result)}]},
            }

        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
