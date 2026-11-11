# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Install the extracted bundle in Relay and check that guarded tools gain the Nomos intent.

The gateway path exercises the worker's LLM request intercept: a request through the gateway must
reach the provider with the required `nomos` object on the guarded tool and not on others. Tool
execution (Council and Executor) is covered by the source's installed-worker acceptance test.
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.environ["REPO_DIR"])
from scripts.smoke import installed_gateway, json_http_fixture, post_json

RESPONSE = {
    "id": "smoke",
    "object": "chat.completion",
    "created": 1,
    "model": "fixture-model",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "bundle-smoke-ok"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


def tool(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "parameters": {"type": "object", "properties": {"arg": {"type": "string"}}, "required": ["arg"]},
        },
    }


def smoke(bundle: Path, relay: Path):
    # Test-only trust anchors (any 32 bytes): the request path never dispatches to a Council or Executor,
    # so no signature is ever checked against them.
    public = os.urandom(32).hex()
    with json_http_fixture(RESPONSE) as provider:
        config = {
            "connection": {"agent_id": "smoke-agent", "council_id": "smoke-council"},
            "guard": {"include": ["run_shell"]},
            "session": {"id": "smoke"},
            "trust": {
                "policy_version": "smoke-only",
                "valid_until": time.time() + 600,
                "council_public_key_hex": public,
                "executors": {"smoke-executor": [public]},
            },
        }
        with installed_gateway(
            bundle, relay, config, gateway_args=("--openai-base-url", provider.url + "/v1")
        ) as gw:
            body = post_json(
                gw + "/v1/chat/completions",
                {
                    "model": "smoke-model",
                    "messages": [{"role": "user", "content": "test"}],
                    "tools": [tool("run_shell"), tool("read_file")],
                },
            )
        if body["choices"][0]["message"]["content"] != "bundle-smoke-ok" or len(provider.calls) != 1:
            raise AssertionError("managed request did not reach the fixture exactly once")
        sent = {t["function"]["name"]: t["function"]["parameters"] for t in provider.calls[0][0]["tools"]}
        if sent["run_shell"]["required"][-1:] != ["nomos"] or "nomos" not in sent["run_shell"]["properties"]:
            raise AssertionError("the worker did not require the Nomos intent on the guarded tool")
        if "nomos" in sent["read_file"]["properties"]:
            raise AssertionError("the worker edited an unguarded tool")


if __name__ == "__main__":
    smoke(Path(os.environ["BUNDLE_DIR"]), Path(os.environ["RELAY_BIN"]))
