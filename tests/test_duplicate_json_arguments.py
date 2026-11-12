# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Reject repeated object members while raw JSON is still available."""

import json
from unittest.mock import AsyncMock

import grpc
import pytest
from pydantic import ValidationError

from nomos.grpc.server import CouncilGrpcProxy
from nomos.grpc.v1 import council_pb2
from nomos.level_0.types.tool_call import ToolCallRequest
from nomos.nat.functions import AuthorizeInput
from nomos.strict_json import strict_json_loads


def _tool_json(arguments: str) -> str:
    return '{"tool_name":"app.call","subject":"user-1","arguments":' + arguments + "}"


def _authorize_json(arguments: str) -> str:
    return '{"agent_id":"agent","tool_call":' + _tool_json(arguments) + "}"


@pytest.mark.parametrize("encoding", [str, bytes, bytearray])
@pytest.mark.parametrize(
    "arguments",
    ['{"secret-member":1,"secret-member":2}', '{"items":[{"x":1,"x":2}]}'],
)
def test_authorize_raw_json_rejects_duplicate_members(encoding, arguments):
    raw = _authorize_json(arguments)
    payload = raw if encoding is str else encoding(raw, "utf-8")
    with pytest.raises(ValidationError) as error:
        AuthorizeInput.model_validate_json(payload)
    assert error.value.errors()[0]["type"] == "json_invalid"
    assert "duplicate JSON object member" in str(error.value)
    assert "secret-member" not in str(error.value)


@pytest.mark.parametrize(
    "raw",
    [
        '{"agent_id":"first","agent_id":"second","command":"true","working_directory":"/"}',
        _tool_json('{"outer":{"x":1,"x":2}}'),
        _tool_json('{"items":[{"x":1,"x":2}]}'),
    ],
)
def test_top_level_and_tool_request_raw_json_reject_duplicates(raw):
    model = AuthorizeInput if '"agent_id"' in raw else ToolCallRequest
    with pytest.raises(ValidationError, match="duplicate JSON object member"):
        model.model_validate_json(raw)


def test_valid_raw_json_preserves_legacy_and_structured_semantics():
    legacy = AuthorizeInput.model_validate_json(
        '{"agent_id":"agent","command":"true","working_directory":"/","env":{"X":"1"}}',
        strict=True,
    )
    assert legacy.command == "true"
    assert legacy.env == {"X": "1"}
    arguments = {"large": 10**100 + 7, "items": [{"x": 1.0}, {"x": 2}]}
    request = AuthorizeInput.model_validate_json(_authorize_json(json.dumps(arguments)))
    assert request.tool_call is not None
    assert request.tool_call.arguments == arguments
    assert type(request.tool_call.arguments["large"]) is int
    tool_request = ToolCallRequest.model_validate_json(_tool_json(json.dumps(arguments)))
    assert tool_request.arguments == arguments
    assert strict_json_loads(json.dumps(arguments)) == arguments


def test_malformed_raw_json_keeps_pydantic_error():
    with pytest.raises(ValidationError) as error:
        AuthorizeInput.model_validate_json("{not json")
    assert error.value.errors()[0]["type"] == "json_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize("generic", [False, True])
@pytest.mark.parametrize("arguments", ['{"x":1,"x":2}', '{"items":[{"x":1,"x":2}]}'])
async def test_grpc_raw_json_rejects_duplicates_before_forwarding(generic, arguments):
    client = AsyncMock()
    context = AsyncMock()
    context.abort.side_effect = RuntimeError("aborted")
    proxy = CouncilGrpcProxy(client, mcp_url="http://council.test/mcp")
    raw = _authorize_json(arguments)
    with pytest.raises(RuntimeError, match="aborted"):
        if generic:
            await proxy.CallTool(
                council_pb2.CallToolRequest(tool_name="council_authorize", raw_json=raw), context
            )
        else:
            await proxy.Authorize(council_pb2.CouncilToolRequest(raw_json=raw), context)
    client.call_tool.assert_not_called()
    assert context.abort.call_args.args[0] == grpc.StatusCode.INVALID_ARGUMENT
    assert "duplicate JSON object member" in context.abort.call_args.args[1]
