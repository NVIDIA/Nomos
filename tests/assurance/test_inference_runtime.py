# SPDX-License-Identifier: Apache-2.0
"""Exercise the installed inference stack through its real HTTP tool-call path."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.mark.parametrize("use_ethos_adapter", [False, True])
def test_function_tool_completion_without_mcp_gateway(monkeypatch, use_ethos_adapter):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    monkeypatch.setenv("LITELLM_TELEMETRY", "False")
    litellm = pytest.importorskip("litellm")
    requests = []

    class Endpoint(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = json.dumps(
                {
                    "id": "tool-completion",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "fixture",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {"name": "finish", "arguments": "{}"},
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    tools = [
        {
            "type": "function",
            "function": {"name": "finish", "parameters": {"type": "object", "properties": {}}},
        }
    ]

    async def complete():
        if use_ethos_adapter:
            pytest.importorskip("nooa")
            from nooa.unifiedllm.unifiedllm import Tool

            from assurance.ethos import InferenceConfig, _build_llm

            client = _build_llm(
                InferenceConfig(
                    model="openai/fixture",
                    api_base=f"http://127.0.0.1:{server.server_port}/v1",
                    api_key="fixture-credential",  # pragma: allowlist secret
                    request_timeout=5,
                ),
                {"calls": 0},
            )
            return await client.acall(
                messages=[{"role": "user", "content": "Call finish."}],
                tools=[
                    Tool(name="finish", description="Finish the analysis.", callable=lambda: None)
                ],
                prompt_cache_key="nooa-agent-strategy",
            )
        return await litellm.acompletion(
            model="openai/fixture",
            api_base=f"http://127.0.0.1:{server.server_port}/v1",
            api_key="fixture-credential",  # pragma: allowlist secret
            messages=[{"role": "user", "content": "Call finish."}],
            tools=tools,
            num_retries=0,
            timeout=5,
        )

    try:
        response = asyncio.run(complete())
        if use_ethos_adapter:
            assert response.tool_calls
        else:
            assert response.choices[0].message.tool_calls[0].function.name == "finish"
        assert len(requests) == 1
        if use_ethos_adapter:
            tool = requests[0]["tools"][0]
            assert tool["type"] == "function"
            assert tool["function"]["name"] == "finish"
            assert tool["function"]["parameters"]["type"] == "object"
            assert requests[0]["parallel_tool_calls"] is False
        else:
            assert requests[0]["tools"] == tools
        assert "prompt_cache_key" not in requests[0]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
