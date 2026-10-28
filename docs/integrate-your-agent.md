<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Integrate your own agent

Keep your agent and its framework. Put a small **adapter** around the place where
it dispatches privileged actions: the adapter asks Nomos first, and only an
approved action runs, exactly as approved. Nomos never calls into your agent and
never runs the action itself; your adapter carries the decision between services.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/diagrams/agent-adapter-dark.svg">
  <img alt="Agent adapter: 1 intercept the agent's tool call; 2 call council_authorize with the action and credentials; 3 if denied, return the guidance to the agent to revise; if approved, 4 POST the Writ to the executor's /v1/writs/execute; 5 keep the signed ExecutionResult and attach it as certified_evidence to a later request." src="assets/diagrams/agent-adapter.svg">
</picture>

## What you need from the operator

| Value | Used for |
|---|---|
| `NOMOS_COUNCIL_URL` | MCP endpoint, for example `http://127.0.0.1:9901/mcp` on a local stack |
| `MONITOR_MCP_PROXY_TOKEN` | HTTP `Authorization: Bearer` header on every MCP request |
| `NOMOS_AGENT_ID`, `NOMOS_AUTH_TOKEN` | The agent's identity, sent inside each tool call's arguments |
| `NOMOS_COUNCIL_ID` | The council to petition (the shipped default is `council-prod`) |
| `NOMOS_EXECUTOR_URL` | The executor that will run approved commands, for example `http://127.0.0.1:9907` |

The account needs the permission scopes its actions require and membership in
the council. On a local stack the operator creates one with
`./scripts/agent_accounts.sh create my-agent --role readonly --council council-prod`
(Compose) or `deploy/kubernetes/scripts/kind-register-agent.sh my-agent council-prod`
(Kind); see [Getting started](getting-started.md#5-manage-agent-accounts).
There are two separate credentials: the proxy token authenticates the
connection, and the agent token identifies the agent inside each request.

## The five steps

1. **Intercept.** Route your agent's privileged shell actions through the adapter
   instead of running them. Pass the exact command, the working directory on the
   executor (not on the agent's machine), and a description, target resource,
   justification and blast radius. The shipped OPA profile requires absolute
   executable paths (`/usr/bin/git`, not `git`).
2. **Authorize.** Call `council_authorize` with those fields as flat arguments.
   One call authenticates, reviews and, on approval, returns the signed `writ`.
   There is no separate fetch or poll step. (`council_propose_intent` reviews
   without minting a Writ.)
3. **Decide.** Continue only on `status: "approved"`. Otherwise hand `guidance`
   and `error` back to the agent. A safer revision is a new request and needs
   fresh authorization. Resubmit the same request after a wait only when the
   response carries `retryable: true`. See
   [denial recovery](agent-denial-recovery.md).
4. **Execute.** POST `{"writ": ...}` unchanged to `/v1/writs/execute` with an
   `X-Agent-ID` header matching the account. HTTP 200 does not mean success:
   check `success`, `verification_stage` (`"passed"` when every check cleared),
   `exit_code` and `error`. A Writ is single-use and short-lived; if the outcome
   is unknown, find out what happened before asking for another.
5. **Attach evidence.** Keep the whole returned `ExecutionResult`. When a later
   action relies on that output, send it in `certified_evidence`. The council
   verifies the executor signature against a registered executor key, the output
   hashes, and the agent and council IDs, and shows it to reviewers as certified
   rather than as the agent's claim.

## A minimal Python adapter

Requires `python -m pip install 'mcp>=1.28.1,<2' httpx`. The example action is
harmless: `echo` is on the default executor's tool list and `/workspace` is its
allowed working directory. In your agent, replace the action with the one it
actually requested.

```python
import asyncio, json, os
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def nomos_action_adapter(session, action, receipts=()):  # 1. Intercept
    reply = await session.call_tool("council_authorize", arguments={  # 2. Authorize
        **action,
        "agent_id": os.environ["NOMOS_AGENT_ID"],
        "auth_token": os.environ["NOMOS_AUTH_TOKEN"],
        "council_id": os.environ["NOMOS_COUNCIL_ID"],
        "certified_evidence": list(receipts),  # 5. Receipts from earlier actions
    })
    if reply.isError:
        raise RuntimeError(reply.content)
    decision = json.loads(reply.content[0].text)
    if decision["status"] != "approved":  # 3. Decide
        return {"denied": decision}  # guidance, error and retryable go back to the agent
    async with httpx.AsyncClient(timeout=240) as client:  # 4. Execute
        response = await client.post(
            os.environ["NOMOS_EXECUTOR_URL"].rstrip("/") + "/v1/writs/execute",
            headers={"X-Agent-ID": os.environ["NOMOS_AGENT_ID"]},
            json={"writ": decision["writ"]},
        )
        response.raise_for_status()
    result = response.json()
    if not result["success"] or result["verification_stage"] != "passed":
        raise RuntimeError(result)
    return result  # 5. Keep the full signed result, unedited

async def main():
    async with streamablehttp_client(
        os.environ["NOMOS_COUNCIL_URL"],
        headers={"Authorization": "Bearer " + os.environ["MONITOR_MCP_PROXY_TOKEN"]},
        timeout=240,  # a council review can outlast the client's 30 s default
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            receipt = await nomos_action_adapter(session, {
                "command": "/usr/bin/echo nomos-integration",
                "working_directory": "/workspace",
                "description": "Check the agent authorization and execution path",
                "target_resource": "/workspace",
                "justification": "Verify this integration with a harmless output command",
                "blast_radius": "Prints a fixed string; no files or services change",
            })
            print(json.dumps(receipt, indent=2))
            # A later action that relies on this output:
            # await nomos_action_adapter(session, next_action, receipts=[receipt])

asyncio.run(main())
```

## Other integration shapes

**IDE agents (Cursor, Windsurf, Copilot, ...).** Connecting an IDE to the council
over MCP exposes the review tools, not execution. Give the agent one command it
may run, a wrapper script that performs steps 2 to 4, and deny direct shell
access in the IDE's settings. The wrapper lives in your project, not in Nomos,
and needs both credentials (proxy token and agent token) and the executor URL.
To keep shell operators from being smuggled through the IDE's allowlist, accept
the command base64-encoded when it contains `|`, `>`, `&&` or `;`.

**Application tools instead of shell commands.** For an API call (open a ticket,
post a message), send a structured `tool_call` with `api_version: "v2"` instead
of `command`. The account needs the `tool:write` scope, and the resulting v2
Writ is verified and dispatched by your application, not by the bundled command
executor. See [MCP tools](reference/mcp-api.md#mcp-tools-reference).

**Your own executor.** Any service that implements `POST /v1/writs/execute`,
`GET /v1/health` and the executor descriptor, verifies the Writ, claims the
nonce in the shared store and signs its results can replace the bundled one.
See the [trusted executor API](reference/mcp-api.md#trusted-executor-api), the
reference implementation in [`executors/docker/`](../executors/docker/), and the
browser guide [Agent integration and custom executors](guides/03-agent-integration.html).
For the council to accept its receipts as certified evidence, the operator
registers its public key (`scripts/agent_accounts.py trusted-executor`).

**Several councils.** Pass `council_id` to choose one; the
[multi-council explorer](interactive/multi-council.html) shows routing and
membership.

## See also

- [Denial recovery](agent-denial-recovery.md): what to do with `denied`, `error` and `retryable`.
- [Council protocol skill](../skills/council-protocol/SKILL.md): loadable instructions that teach an LLM agent to write truthful, reviewable requests.
- [MCP request builder](interactive/mcp-integration.html) (browser): build tool calls interactively.
- [Certified evidence verification](interactive/certified-evidence-verification.html) (browser): how receipts are checked.
