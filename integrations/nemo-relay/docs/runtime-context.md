<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Runtime context from a trusted application

The worker can carry an application's runtime-location facts to Nomos for one
guarded local tool call. This lets Council compare a logical target such as
`/workspace/project/reports` with an actual execution root such as
`/srv/workspaces/project`, without asking the model to guess the host's paths.
It does not authorize execution or assert human approval.

## Before and after

Without runtime context, Council might see a declared target under
`/workspace/project` and a tool operating in `/srv/workspaces/project`. It has
no reason to assume those roots correspond.

With context admitted by both the worker and Council, the review includes:

```yaml
source: example.application
runtime:
  working_directory: /srv/workspaces/project
  workspace_mappings:
    - logical_root: /workspace/project
      execution_root: /srv/workspaces/project
```

The application owns and validates this mapping. The model's review fields and
the exact executable arguments are left unchanged. Runtime facts come from
host/backend state, not from a guess based on a tool argument's name.

## Enable both trust gates

The worker rejects supplied runtime context unless its operator enables the
exact source and tools in Relay's `plugins.toml`, inside the existing Nomos
plugin entry:

```toml
[plugins.dynamic.config.runtime_context]
source = "example.application"
tools = ["files.run"]
```

Merge this into the existing entry; do not duplicate TOML table headers. Each
listed tool must already be guarded and use `executor = "local"`. Omit the
`runtime_context` table to keep the feature disabled. This is distinct from
the [host human-approval configuration](host-human-approval.md).

The Council must independently trust the worker's authenticated account, source
and exact tool in its `council.yaml`:

```yaml
require_auth: true
runtime_context:
  trusted_hosts:
    - agent_id: example-agent
      source: example.application
      tools: [files.run]
```

`example-agent` is the account used by the worker's Council connection, not a
model-supplied user identifier. The Council default is an empty trusted-host
list. Its normal account permissions, membership and policy checks still apply.
A source identifier is not a credential. Relay transports application data;
it does not authenticate the assertions inside it. Enable this feature only
when the application and its relevant extensions are trusted to supply facts.

## Host-to-worker payload

Pass runtime facts through Relay's separate application execution context,
never through tool arguments, prompt text or telemetry metadata. In the Node
typed API this is the final options object's `executionContext`; Python callers
use the `execution_context` keyword. The worker receives `ctx.execution_context`.

```json
{
  "data": {
    "runtime_context": {
      "version": 1,
      "source": "example.application",
      "tool_name": "files.run",
      "tool_call_id": "call-example",
      "arguments": {
        "script": "mkdir reports",
        "directory": "/srv/workspaces/project"
      },
      "runtime": {
        "working_directory": "/srv/workspaces/project",
        "workspace_mappings": [
          {
            "logical_root": "/workspace/project",
            "execution_root": "/srv/workspaces/project"
          }
        ]
      }
    }
  }
}
```

`arguments` must be a snapshot of the **full actual business arguments** passed
to the worker, excluding only the added `nomos` review object. Include any
host-injected execution fields that are present in that payload; this example's
tool happens to have only `script` and `directory`. It is not enough to copy
only the path or command that seems relevant. The host must use the same
tool-call ID passed to Relay for this invocation.

The worker checks source and exact tool, validates the version and runtime
shape, and compares the tool-call ID and complete arguments. Argument equality
uses Nomos canonical argument bytes, including integer-versus-float spelling;
it does not coerce values or accept a host-provided digest instead of the
snapshot. Changing facts for an already-used call ID also changes the worker's
request binding, so a previous outcome cannot be reused as if nothing changed.
Malformed, mismatched or untrusted supplied context blocks the call; it is not
silently dropped to continue without it.

The complete execution context must fit Relay's 16 KiB JSON and 64-container-level
limits. The duplicate arguments, runtime context, any separate `host_approval`
and other application data share that budget. Reject an oversized payload;
never truncate the snapshot or omit context as a fallback. Runtime paths are
absolute, bounded and free of control characters or dot segments. At most 16
workspace mappings are accepted, with no duplicate or overlapping logical roots.

## Worker-to-Council payload

After validating the per-call binding, the worker forwards only this context
inside the structured authorization request's `tool_call.runtime_context`:

```json
{
  "version": 1,
  "source": "example.application",
  "runtime": {
    "working_directory": "/srv/workspaces/project",
    "workspace_mappings": [
      {
        "logical_root": "/workspace/project",
        "execution_root": "/srv/workspaces/project"
      }
    ]
  }
}
```

The duplicate `arguments`, `tool_name` and `tool_call_id` binding fields do not
become fields in the core context. The normal structured request still carries
its actual tool name and arguments. The core authenticates the requesting agent
and applies its independent issuer/source/tool allowlist before presenting facts
to OPA, Council and the optional decision-model stage.

## Boundaries and privacy

Runtime context follows normal authorization. It does not skip OPA or Council,
invoke the continuation, replace signed argument checks, or make an unresolved
denial into human approval. A separate valid `host_approval` assertion can coexist
with it, but that assertion must pass its own independent checks.

The host remains responsible for validating its folder/resource grant,
resolving paths safely, and rechecking relevant state immediately before an
effect. Neither the worker nor Council verifies the remote filesystem merely by
accepting a path. A working directory is an initial location, not a confinement
mechanism, proof of file existence, execution evidence or a completed approval.

The core holds admitted facts as live private review material rather than
adding them to serialized `Intent` data or the signed operation. Reviewers can
still quote physical paths in their reasons or guidance, and those outputs may
be persisted. Do not claim that this feature guarantees path secrecy, or put
credentials in runtime facts. Apply the deployment's data-handling requirements
to the configured review services and their outputs.

This requires the matching Relay execution-context transport and updated worker
and core implementations. See [source-build testing](relay-1-source-testing.md)
for the current development-build requirements; this page is not a claim that
matching published packages are available.
