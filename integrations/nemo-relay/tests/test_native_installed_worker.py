# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Native correlation through the installed gRPC worker, not in-process callback mocks."""

import asyncio
import json
import os
from pathlib import Path

import pytest

if not os.getenv("NOMOS_TEST_RELAY_BIN"):
    pytest.skip("opt-in installed Relay acceptance test", allow_module_level=True)

from nemo_relay import LLMRequest, ScopeType, ToolExecutionResult, llm, plugin, scope, tools
from nemo_relay.codecs import OpenAIChatCodec
from support import INTENT, LOCAL_TOOL, TOOL, base_config, call
from test_installed_worker import Services, install, run

pytestmark = pytest.mark.installed


def raw_response(call_id, name="read_file"):
    """Encode provider tool calls in the actual OpenAI chat-completion response shape."""
    return {
        "id": "response-" + call_id,
        "model": "fixture",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps({"path": "a", "nomos": INTENT}),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }


def native_config():
    """Configure a managed worker to guard only the local fixture tool."""
    return base_config(
        guard={"include": ["read_file", "run_shell"]},
        tools={
            "read_file": {"executor": "local"},
            "run_shell": {"executor": "nomos_http", "command_argument": "command"},
        },
    )


async def test_native_installed_worker_correlates_overlapping_requests_and_complete_stream(
    tmp_path, monkeypatch
):
    """Verify native correlation through real managed-worker unary and streaming callbacks."""
    cfg = native_config()
    async with Services() as services:
        cfg["connection"].update(mcp_url=services.url + "/mcp", executor_url=services.url)
        registry = install(tmp_path, cfg, monkeypatch)
        activation = await plugin.initialize(plugin.PluginConfig(), str(registry))
        owner = scope.push("synthetic-agent", ScopeType.Agent)
        requests, executed, finished = [], [], []
        b_finished = asyncio.Event()

        async def model(label):
            """Expose a synthetic provider through the real Relay model execution path."""

            async def provider(request):
                """Return controlled provider output without making external model requests."""
                requests.append(request.content)
                if label == "A":
                    await b_finished.wait()
                return raw_response("call_" + label)

            await llm.execute(
                "fixture",
                LLMRequest(
                    {},
                    {
                        "model": "fixture",
                        "messages": [{"role": "user", "content": "request " + label}],
                        "tools": [LOCAL_TOOL],
                    },
                ),
                provider,
                codec=OpenAIChatCodec(),
                response_codec=OpenAIChatCodec(),
            )
            finished.append(label)
            if label == "B":
                b_finished.set()

        async def local(args):
            """Record actual local execution so denied or replayed calls are detectable."""
            executed.append(args)
            return ToolExecutionResult({"content": "synthetic file"})

        try:
            await asyncio.gather(model("A"), model("B"))
            assert finished == ["B", "A"]
            for prepared in requests:
                schema = prepared["tools"][0]["function"]["parameters"]["properties"]["nomos"]
                assert "turn" not in schema["properties"] and "nt_" not in json.dumps(prepared)
            for label in ["A", "B"]:
                result = await run(call(name="read_file", call_id="call_" + label, path="a"), local)
                assert result.result["status"] == "executed", result.result
                assert "request " + label in services.council.calls[-1]["context"]

            async def source(request):
                """Yield fixture chunks while preserving cancellation and cleanup behavior."""
                requests.append(request.content)
                arguments = json.dumps({"path": "a", "nomos": INTENT})
                for delta, finish in [
                    (
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_stream",
                                    "type": "function",
                                    "function": {"name": "read_file", "arguments": arguments[:8]},
                                }
                            ]
                        },
                        None,
                    ),
                    ({"tool_calls": [{"index": 0, "function": {"arguments": arguments[8:]}}]}, "tool_calls"),
                ]:
                    yield {
                        "id": "response-stream",
                        "model": "fixture",
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                    }

            chunks = []
            stream = await llm.stream_execute(
                "fixture",
                LLMRequest(
                    {},
                    {
                        "model": "fixture",
                        "messages": [{"role": "user", "content": "stream request"}],
                        "tools": [LOCAL_TOOL],
                    },
                ),
                source,
                chunks.append,
                lambda: {"choices": []},
                codec=OpenAIChatCodec(),
                response_codec=OpenAIChatCodec(),
            )
            async for _ in stream:
                pass
            result = await run(call(name="read_file", call_id="call_stream", path="a"), local)
            assert result.result["status"] == "executed", result.result
            assert "stream request" in services.council.calls[-1]["context"]
            assert executed == [{"path": "a"}] * 3
            assert len(services.council.calls) == 3

            # An ordinary call does not require a model-origin binding or Council.
            plain = await tools.execute("plain_read", {}, local, tool_call_id="plain")
            assert plain.result == {"content": "synthetic file"}
            assert len(services.council.calls) == 3
        finally:
            scope.pop(owner)
            await activation.close()


async def test_native_installed_worker_denies_without_invoking_local_continuation(tmp_path, monkeypatch):
    """Keep an installed-worker Council denial from reaching the actual local tool."""
    cfg = native_config()
    cfg["council_context"] = {"include_user_request": False}
    async with Services() as services:
        cfg["connection"].update(mcp_url=services.url + "/mcp", executor_url=services.url)
        registry = install(tmp_path, cfg, monkeypatch)
        activation = await plugin.initialize(plugin.PluginConfig(), str(registry))
        owner = scope.push("synthetic-agent", ScopeType.Agent)
        try:

            async def provider(request):
                """Return controlled provider output without making external model requests."""
                return raw_response("denied", "run_shell")

            await llm.execute(
                "fixture",
                LLMRequest(
                    {},
                    {
                        "messages": [{"role": "user", "content": "must not appear in Council context"}],
                        "tools": [TOOL],
                    },
                ),
                provider,
                codec=OpenAIChatCodec(),
                response_codec=OpenAIChatCodec(),
            )
            denied = await run(
                call(name="run_shell", call_id="denied", command="/bin/rm /workspace/synthetic-marker")
            )
            assert denied.result["status"] == "denied", denied.result
            assert len(services.council.calls) == 1 and services.results == []
            assert "must not appear" not in services.council.calls[0].get("context", "")
            assert "latest user request" not in services.council.calls[0].get("context", "")
        finally:
            scope.pop(owner)
            await activation.close()


@pytest.mark.parametrize("stream_format", ["auto", "openai_chat"])
async def test_node_callback_codec_requires_explicit_stream_format(tmp_path, monkeypatch, stream_format):
    """Exercise Node's callback codec API against the installed worker with synthetic chunks."""
    if not os.getenv("NOMOS_TEST_NODE_RELAY_MODULE"):
        pytest.skip("set NOMOS_TEST_NODE_RELAY_MODULE to an installed nemo-relay-node module")
    cfg = native_config()
    cfg["stream_format"] = stream_format
    async with Services() as services:
        cfg["connection"].update(mcp_url=services.url + "/mcp", executor_url=services.url)
        registry = install(tmp_path, cfg, monkeypatch)
        fixture = tmp_path / "fixture.json"
        fixture.write_text(json.dumps({"tool": LOCAL_TOOL, "intent": INTENT}))
        env = dict(
            os.environ,
            PROBE_REGISTRY=str(registry),
            PROBE_FIXTURE=str(fixture),
            PROBE_EXPECT_STREAM_REJECTION="1" if stream_format == "auto" else "0",
        )
        process = await asyncio.create_subprocess_exec(
            "node",
            str(Path(__file__).parent / "fixtures/native_node_host.cjs"),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 90)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        assert process.returncode == 0, stderr.decode()[-4000:]
        result = json.loads(stdout.decode().splitlines()[-1])
        assert result["unary"] == "passed"
        assert result["stream"] == (
            "passed" if stream_format == "openai_chat" else "rejected before provider"
        )
        assert (
            len(services.council.calls)
            == result["localExecutions"]
            == (2 if stream_format == "openai_chat" else 1)
        )
        assert "node unary" in services.council.calls[0]["context"]
        if stream_format == "openai_chat":
            assert "node stream" in services.council.calls[1]["context"]


@pytest.mark.parametrize("allow_single_agent_root", [False, True])
async def test_node_reconstructed_agent_requires_explicit_host_assertion(
    tmp_path, monkeypatch, allow_single_agent_root
):
    """Exercise real reconstructed H2/stream/H3 scopes across concurrent Agents."""
    if not os.getenv("NOMOS_TEST_NODE_RELAY_MODULE"):
        pytest.skip("set NOMOS_TEST_NODE_RELAY_MODULE to an installed nemo-relay-node module")
    cfg = native_config()
    cfg["stream_format"] = "openai_chat"
    cfg["correlation"] = {"allow_single_agent_root": allow_single_agent_root}
    async with Services() as services:
        cfg["connection"].update(mcp_url=services.url + "/mcp", executor_url=services.url)
        registry = install(tmp_path, cfg, monkeypatch)
        fixture = tmp_path / "fixture.json"
        fixture.write_text(json.dumps({"tool": LOCAL_TOOL, "intent": INTENT}))
        env = dict(
            os.environ,
            PROBE_REGISTRY=str(registry),
            PROBE_FIXTURE=str(fixture),
            PROBE_ALLOW_SINGLE_ROOT="1" if allow_single_agent_root else "0",
        )
        process = await asyncio.create_subprocess_exec(
            "node",
            str(Path(__file__).parent / "fixtures/native_node_propagation_host.cjs"),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 90)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        assert process.returncode == 0, (stdout + stderr).decode()[-6000:]
        assert len(services.council.calls) == (4 if allow_single_agent_root else 0)
        if allow_single_agent_root:
            contexts = [c["context"] for c in services.council.calls]
            assert sum("request A" in c for c in contexts) == 2
            assert sum("request B" in c for c in contexts) == 2
