# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


import json

import pytest

from nomos.grpc.mcp_client import MCPResponseError, parse_mcp_tool_result


class _TextContent:
    def __init__(self, text: str):
        self.text = text


class _ToolResult:
    def __init__(self, text: str):
        self.content = [_TextContent(text)]


def test_parse_mcp_tool_result_preserves_json_object_and_raw_text():
    raw = json.dumps(
        {
            "status": "approved",
            "writ_id": "writ-123",
            "council_members": [{"member_id": "command-integrity", "decision": "approve"}],
        },
        separators=(",", ":"),
    )

    result = parse_mcp_tool_result(_ToolResult(raw))

    assert result.raw_json == raw
    assert result.data["status"] == "approved"
    assert result.data["council_members"][0]["member_id"] == "command-integrity"


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        '["valid json", "but not object"]',
        '"valid string but not object"',
    ],
)
def test_parse_mcp_tool_result_rejects_non_object_payloads(payload):
    with pytest.raises(MCPResponseError):
        parse_mcp_tool_result(_ToolResult(payload))


@pytest.mark.parametrize(
    "result",
    [
        object(),
        type("NoContent", (), {"content": []})(),
        type("NoText", (), {"content": [object()]})(),
    ],
)
def test_parse_mcp_tool_result_rejects_unexpected_shapes(result):
    with pytest.raises(MCPResponseError):
        parse_mcp_tool_result(result)
