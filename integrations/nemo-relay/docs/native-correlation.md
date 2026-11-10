<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Associate tool calls with their model request without asking the model to copy an ID

The worker needs to know which model request produced a proposed tool call. That request supplies the optional
user-request text and offered tool definitions used in Council review. Picking the most recent request is unsafe:
two requests can run concurrently and finish in the opposite order.

The worker now associates the provider's returned tool-call ID with its originating request inside **H2: the LLM
execution interceptor**. The model supplies the business intent; it no longer carries Nomos correlation metadata.

## Hook names used in this guide

H1/H2/H3 are this project's shorthand, not Relay API names or three callbacks that all remain installed.

| Label | Role in this integration |
|---|---|
| **H1: request-only interceptor** | Removed. It could capture a request but could not observe its returned tool-call IDs. |
| **H2: LLM execution interceptor** | Capture the snapshot before model dispatch; associate the completed response's tool-call IDs afterward. |
| **H3: tool execution interceptor** | Retrieve the originating snapshot and authorize the actual proposed operation. |

H2 has two registered callbacks: `correlate-request` for non-streaming calls and `correlate-stream` for streaming
calls. The callback names remain unchanged. Capturing happens at H2 entry; binding happens at H2 completion.
H3 does not create the request snapshot.

## The user-visible problem

Model-managed correlation can expose internal bookkeeping and encourage incorrect reuse. For example, after a
user asks an assistant to send a message, the assistant might respond:

> I'll use the new Nomos turn value (`nt_<request-token>`) on any governed call from here on.

This example is illustrative; the token is a placeholder. A token issued for one model request must not become
a promise about future requests.

Previously the worker generated a fresh token for each model request and instructed the model to copy it.
One human message can cause several model requests, so that token was never a promise about all future calls.
Removing the field and its instructions removes that source of bookkeeping leakage. It does not promise that
an assistant can never repeat tracking text already present in its historical conversation.

## Before and after

Before, the prompt included a per-request instruction:

```text
Nomos turn: nt_<request-token>. Put this exact value in nomos.turn on every governed tool call ...
```

The model then had to propose business arguments **and** copy that token correctly:

```json
{
  "path": "/workspace/notes.txt",
  "nomos": {
    "turn": "nt_<request-token>",
    "description": "Read the requested notes file",
    "justification": "The user asked to read these notes",
    "target_resource": "/workspace/notes.txt",
    "blast_radius": "Read-only access to one file"
  }
}
```

After, the same tool proposal contains four required intent fields and no tracking field:

```json
{
  "path": "/workspace/notes.txt",
  "nomos": {
    "description": "Read the requested notes file",
    "justification": "The user asked to read these notes",
    "target_resource": "/workspace/notes.txt",
    "blast_radius": "Read-only access to one file"
  }
}
```

Optional `nomos.evidence` remains available with a signing key. A `turn` property is now an unexpected intent
field. There is no legacy mode, prompt instruction to copy a token, or fallback to the latest request.

## How association works

**Agent scope** means a host-owned execution boundary in Relay. Its UUID distinguishes Agents sharing one worker;
it is not an authenticated user account or a permanent chat identifier. **Snapshot** means one immutable internal
request record: its Agent owner, optional bounded user-request text, and optional offered tool definitions. It is
not a copy of the entire conversation. **Tool-call ID** means the identifier observed in the provider's response
and supplied by the host again when executing that proposal.

**Figure 1. Native association from a model request to Council review.**

```mermaid
sequenceDiagram
    participant Host
    participant Worker
    participant Model
    participant Store
    participant Council
    Host->>Worker: H2 entry: native Agent scope + request
    Worker->>Store: Capture request A; prepare intent schema/catalog
    Worker->>Model: next.call(prepared request)
    Model-->>Worker: Completed proposal with call_A
    Worker->>Store: H2 completion: bind (Agent UUID, call_A) to snapshot A
    Worker-->>Host: Return response unchanged
    Host->>Worker: H3: tool_call_id=call_A + actual tool arguments
    Worker->>Store: Resolve the exact originating snapshot
    Worker->>Council: Review actual operation and originating request context
    Council-->>Worker: Approval or denial
```

The unary H2 execution callback retains its own snapshot while awaiting the response, decodes that response using
the host-provided response codec, and binds all returned call IDs before returning. Request B can finish before
request A without changing either association. Several calls in one response share that response's snapshot.

All response IDs are bound, including calls to discovery or execution wrappers. A host may resolve a wrapper
name into a guarded leaf tool before H3. The model-facing name is provenance, not permission: Council still
reviews H3's actual tool name and held business arguments, and the writ must match that operation. One provider
call ID represents one logical execution; reusing it for several different leaf operations is unsupported.

## Streaming: an explicit format contract

Relay SDK 0.11.1 supplies raw stream chunks to the worker's H2 streaming execution callback, without a response
codec there. The current collector supports **OpenAI chat-completion chunks only**. It retains bounded call IDs
and tool-name fragments, not arguments, assistant prose, or whole chunks.

Merge the following settings into the existing Nomos `[[plugins.dynamic]]` entry. Add keys to tables
that already exist rather than repeating their headers.

```toml
[plugins.dynamic.config]
# Default: require Relay to identify its built-in OpenAI chat codec.
stream_format = "auto"

# Alternatively, for a host that guarantees OpenAI chat chunks but exposes
# its codec through callbacks rather than a built-in codec identity:
# stream_format = "openai_chat"

[plugins.dynamic.config.council_context]
include_user_request = false
```

`stream_format = "openai_chat"` is the operator's declaration of the wire format; it is not automatic format
detection. Do not enable it for another provider format. The same shape, count, identity and completion checks
still apply. Unary calls use their response codec and do not need this streaming declaration.

The worker publishes stream bindings only after terminal `finish_reason: "tool_calls"` for each proposed call's
choice, normal stream exhaustion, and successful source cleanup. Call-name and argument fragments can arrive
in separate chunks. Cancellation, a broken stream, invalid identities, or cleanup failure publishes no bindings
for that response. The host must finish consuming the stream before executing its proposed tools.

## Safety and integration requirements

- Hosts that cache or generate a separate intent/admission schema must regenerate it to remove `turn`;
  deployments must update their schema and worker together.
- The host must preserve the same native Agent UUID and provider tool-call ID between model execution and H3.
  Missing, expired, foreign-Agent or ambiguous bindings refuse the guarded call before Council authorization.
- Distinct Agents can use the same provider call ID without sharing snapshots or evidence. A second response
  reusing an ID inside the same Agent invalidates that existing binding instead of silently replacing it.
- Native identities select context; they do not grant permission. Guard policy, exact-operation writ checks,
  signature validation, executor trust, replay protection and Council approval remain required.
- Bindings expire with their request records and are bounded by global and per-Agent count/storage quotas.
  Optional tool definitions leave room for a proposal binding. Quota exhaustion blocks publication or execution.
- Evidence visibility and denial state are isolated by native Agent UUID. A newly created Agent does not inherit
  another Agent's evidence merely because the host copies its transcript. Stable history across separate Agent
  lifetimes requires a separate trusted host contract; it is not inferred from model text.
- A human-approval replay must restore the original native Agent identity and call ID. A replay under a fresh
  Agent, or after worker restart, cannot reconstruct this association from business arguments. It refuses.
- Agent scope is not user identity. A configured static `tool_call.subject` still assumes one subject per worker
  activation; this change does not make a shared multi-user deployment safe automatically.
- With `council_context.include_user_request = false`, user-request text is neither retained in snapshots nor
  appended to Council context. Operational context and explicitly configured certified evidence remain separate.
- Unguarded tools still pass through; this change does not add completion records for unguarded calls.

## Temporary compatibility for a reconstructed Agent root

Strict mode remains the default. It requires an application Agent beneath the root. Some hosts isolate an
operation by capturing propagation and reconstructing a new stack:

```text
Original:       [root R, application Agent G]
Reconstructed:  [Agent G]
```

The application UUID G survives, but its ancestry is absent. A controlled deployment can opt in to accepting
**exactly one Agent-labelled entry**, using an operator setting in its installed worker configuration:

```toml
[plugins.dynamic.config.correlation]
# Host assertion: every singleton root is the genuine application Agent UUID,
# preserved across model/tool calls, concurrent operations and approval replay.
# False by default. Enable only after verifying this contract in your host.
allow_single_agent_root = true
```

This is a host/operator assertion, not provenance the worker can verify. Propagation from a Function F can
also produce a synthetic `[Agent F]`; the same setting would accept F. Do not enable it where arbitrary parent
scopes can become roots, or where different Agents can share a reconstructed identity. Test the actual callback
snapshots, not only the stack before dispatch. A root with non-Agent descendants is still rejected unless a
separate application Agent exists beneath it. Malformed UUIDs and unavailable snapshots remain invalid.

The exception applies consistently to unary/streaming model interception and guarded H3. It does not replace
`(Agent UUID, provider tool-call ID)` lookup with a latest-snapshot guess. It does not authenticate a user,
restore expired/restarted bindings, skip Council, change writ checks, or solve the static-subject deployment
constraint. Denied calls still cannot execute through the continuation.

The installed Node-worker regression uses separate reconstructed stacks for model and tool operations. It
checks two concurrent Agents using identical provider call IDs, unary and streaming proposals, and unguarded
passthrough. Strict mode refuses before the synthetic provider; opt-in mode preserves per-Agent context and
executes each approved callback once. This does not establish live application or human-approved replay compatibility.

Replace this compatibility assertion with Relay's supported identity-preserving Node context-fork API when
available and verified. Until then, hosts that cannot establish the UUID contract must keep strict mode and
remain blocked. No release date or API availability is assumed by the worker.

## Reproduce the checks

Requires Python 3.11+, Relay 0.11+, and the pinned SDK/CLI/binding 0.11.1 test group.

```sh
make sync
make test
make lint
```

The 35-second Council-latency regression is opt-in: `NOMOS_TEST_SLOW=1 make test`. Ordinary test and commit
checks do not enable it.

The focused tests are [request/response correlation](../tests/test_native_correlation.py),
[bounded stream/identity parsing](../tests/test_native_context.py), and
[installed worker acceptance](../tests/test_native_installed_worker.py).

For Node callback codecs, also set `NOMOS_TEST_NODE_RELAY_MODULE` to an installed `nemo-relay-node` 0.11.1 module
and ensure Node is on `PATH`. The installed test checks both automatic rejection and explicitly declared OpenAI
streaming. Its [Node fixture](../tests/fixtures/native_node_host.cjs) uses synthetic provider responses, a local
tool with no external effects, and a loopback Council with test-only signing keys.

These tests verify the real managed worker and host bindings. They are not a live model-quality evaluation,
a real message send, or proof of a particular application's human-approval replay integration.
