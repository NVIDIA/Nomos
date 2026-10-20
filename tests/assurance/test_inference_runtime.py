# SPDX-License-Identifier: Apache-2.0
"""Exercise the installed inference stack through its real HTTP tool-call path."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


def test_function_tool_completion_without_mcp_gateway(monkeypatch):
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
        assert response.choices[0].message.tool_calls[0].function.name == "finish"
        assert len(requests) == 1
        assert requests[0]["tools"] == tools
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
