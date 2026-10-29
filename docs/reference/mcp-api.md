<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# MCP tools and executor API

Reference for the council's MCP tools and the trusted executor's HTTP API. For a
guided integration, start with [Integrate your own agent](../integrate-your-agent.md).

The gRPC proxy (`nomos/grpc/v1/council.proto`) maps a subset of these tools;
see the proto for the exact list.

## MCP Tools Reference

The council exposes eighteen MCP tools via NVIDIA NeMo Agent Toolkit:
authorization/status operations, explicit v1/v2 aliases for authorize and
propose, signed-ledger reads, and first-class dynamic council management tools.
Connect with any MCP client:

```python
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

# PROXY_TOKEN is MONITOR_MCP_PROXY_TOKEN from deploy/.env. Port 9901 is the
# monitor's /mcp proxy, which returns 401 without the Bearer header.
async with streamablehttp_client(
    "http://localhost:9901/mcp",
    headers={"Authorization": f"Bearer {PROXY_TOKEN}"},
    timeout=240,  # the client default (30s) can cut off a healthy council review
) as (r, w, _):
    async with ClientSession(r, w) as session:
        await session.initialize()
        result = await session.call_tool("council_authorize", arguments={...})
```

| Tool | Purpose | Key Parameters |
|------|---------|----------------|
| `council_authorize` | **Full authorization flow** — authenticate, evaluate, mint writ | common intent fields plus either `command` + `working_directory`, or structured `tool_call`; optional `certified_evidence` (earlier signed `ExecutionResult`s) |
| `council_propose_intent` | Submit intent for evaluation (without minting writ) | `auth_token`, `agent_id`, `command`, `description`, `target_resource`, `justification` |
| `council_v1_authorize` / `council_v1_propose_intent` | Explicit v1 aliases | Same as `council_authorize` / `council_propose_intent` |
| `council_v2_authorize` / `council_v2_propose_intent` | Explicit multi-council tools | Same as above, with required `council_id` |
| `council_check_status` | Check intent/workflow status | `intent_id` |
| `council_get_public_key` | Get council's Ed25519 public key | — |
| `council_check_writ_status` | Check writ lifecycle (valid/expired/used) | `writ_id` |
| `council_pending_approvals` | List intents awaiting approval | — |
| `council_ledger_page` | Page through signed ledger entries, newest first | `council_id`, `cursor_sequence`, `page_size` |
| `council_ledger_get` | Get one signed ledger entry by sequence | `council_id`, `sequence` |
| `council_list_councils` | List configured dynamic council profiles | optional `management_token` |
| `council_show_council` | Describe one council profile | `council_id`, optional `management_token` |
| `council_create_council` | Create a generated profile from a server-side template | `management_token`, `council_id`, `template`, `enabled` |
| `council_put_council_profile` | Create or replace a full structured profile | `management_token`, `council_id`, `profile`, optional `enabled` |
| `council_set_council_enabled` | Enable or disable a dynamic council | `management_token`, `council_id`, `enabled` |
| `council_validate_catalog` | Validate the catalog and referenced profiles | optional `management_token` |

Application-owned tools use a structured request instead of pretending the
tool call is a shell command. Nomos validates and snapshots `arguments`, derives
their `nomos-json-v1` SHA-256 digest, and returns a signed v2 writ bound to that
digest. The caller does not supply the digest or an API-version switch:

```json
{
  "auth_token": "<registered-agent-token>",
  "agent_id": "app-service",
  "council_id": "council-prod",
  "description": "Send the requested Slack message",
  "justification": "The user requested this external write",
  "tool_call": {
    "schema_version": 1,
    "tool_name": "slack.send_message",
    "arguments": {"channel": "C123", "message": "hello"},
    "subject": "opaque-host-derived-subject"
  }
}
```

`nomos-json-v1` uses Python's `json.dumps` numeric encoding: integers retain
exact precision up to 640 decimal digits per integer (excluding the sign);
`1` and `1.0` produce different digests; negative float zero
is `-0.0`; and exponent spelling follows Python (for example, `1e-07`). Object
keys are sorted, whitespace is omitted, Unicode is UTF-8, and non-finite numbers
are rejected. This contract currently targets Python clients; clients in other
languages must reproduce these bytes exactly, including numeric types and
formatting. It is not RFC 8785/JCS.

The integer bound is enforced before decimal serialization and is shared with
the companion plugin. It stays within Python's minimum configurable decimal
conversion limit without changing interpreter-wide settings. Positive and
negative 640-digit integers are accepted; 641-digit integers are rejected,
including inside nested objects and arrays. The existing byte, depth, and node
bounds still apply.

Raw JSON passed to `AuthorizeInput.model_validate_json`,
`ToolCallRequest.model_validate_json`, or the gRPC `raw_json` adapter rejects
duplicate object members at every nesting level before dictionary construction.
Already decoded MCP/Relay dictionaries and protobuf `Struct` values cannot
preserve duplicate members; their upstream raw JSON decoders must enforce the
same rule. Nomos cannot recover duplicates discarded before those boundaries.

The v2 signature covers the original bytes decoded from `payload_base64`.
Verify those bytes directly before trusting their contents; do not parse and
reserialize the payload for signature verification. A valid signature alone
does not authorize a dispatch. Before running the held tool call, the
application must also:

1. Match `operation.tool_name` and `operation.subject` to the held call, and
   recompute `operation.arguments_sha256` over its arguments
   (`nomos-json-v1` canonicalization).
2. Check that `target_agent_id` is the calling agent, `council_id` is the
   expected council, and the current time lies between `issued_at` and
   `expires_at`.
3. Claim `nonce` atomically in the shared nonce store, and refuse the call if
   it was already claimed.

The authenticated agent needs the dedicated `tool:write` scope. Existing shell
requests and trusted-executor writs retain their current command contract.

Mutating dynamic-council tools are fail-closed unless the service environment
sets `COUNCIL_MANAGEMENT_TOKEN` or `NOMOS_COUNCIL_MANAGEMENT_TOKEN`. To require
that token for `council_list_councils`, `council_show_council`, and
`council_validate_catalog`, set `allow_unauthenticated_read: false` on each
corresponding tool configuration.

### Trusted Executor API

Executors conform to the trusted executor contract. The bundled
`executors/docker/` service is a local subprocess example, and
`executors/openshell/` is an OpenShell backend adapter. Production deployments
can replace either with any service that implements the same endpoints.

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/v1/writs/execute` | POST | Submit a signed writ for execution. Body: `{"writ": "<base64 blob>"}` plus `X-Agent-ID: <agent id>` |
| `/v1/health` | GET | Health check with contract version, capabilities, uptime, and key fingerprint |
| `/.well-known/nomos/trusted-executor.json` | GET | Machine-readable contract descriptor |
| `/execute`, `/health` | POST/GET | Legacy aliases kept for existing clients |

Nomos executors generate an executor-owned Ed25519 key and attach signed
execution evidence to responses. The evidence binds the writ id, exact approved
command, attachment hashes, stdout/stderr hashes, exit code, and verification
stage so clients can detect tampering after the fact.

---
