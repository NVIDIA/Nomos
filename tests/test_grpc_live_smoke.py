# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Optional live smoke tests for a deployed council gRPC proxy.

Set NOMOS_GRPC_SMOKE_ADDR, for example `127.0.0.1:9904`, to enable.
"""

import json
import os

import grpc
import pytest

from nomos.grpc.v1 import council_pb2, council_pb2_grpc

SMOKE_ADDR = os.environ.get("NOMOS_GRPC_SMOKE_ADDR", "")

pytestmark = pytest.mark.skipif(
    not SMOKE_ADDR,
    reason="set NOMOS_GRPC_SMOKE_ADDR to run live gRPC smoke tests",
)


@pytest.fixture
def live_stub():
    channel = grpc.insecure_channel(SMOKE_ADDR)
    try:
        grpc.channel_ready_future(channel).result(timeout=5)
        yield council_pb2_grpc.CouncilProxyStub(channel)
    finally:
        channel.close()


def test_live_grpc_health(live_stub):
    result = live_stub.Health(council_pb2.HealthRequest(), timeout=10)

    assert result.ok is True
    assert result.mcp_url
    assert result.error == ""


def test_live_grpc_get_public_key(live_stub):
    result = live_stub.GetPublicKey(council_pb2.CouncilToolRequest(), timeout=10)
    payload = json.loads(result.raw_json)

    assert result.tool_name == "council_get_public_key"
    assert len(payload["public_key_hex"]) == 64
    assert len(payload["fingerprint"]) == 64


def test_live_grpc_generic_call_tool_get_public_key(live_stub):
    result = live_stub.CallTool(
        council_pb2.CallToolRequest(tool_name="council_get_public_key"),
        timeout=10,
    )

    assert result.tool_name == "council_get_public_key"
    assert "public_key_hex" in json.loads(result.raw_json)
