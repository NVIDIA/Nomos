<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Host-reported runtime context for tool review

An application may show the model a logical workspace while executing a tool
against a different filesystem root. Nomos can accept an explicit, operator-trusted
mapping so reviewers can compare the model's declared target with the actual
operation. The feature is disabled by default.

## Why a mapping matters

Suppose the model requests a new directory under `/workspace/project`, and the
host resolves that workspace to `/srv/workspaces/project` before review:

```yaml
target_resource: /workspace/project/reports
tool_call:
  tool_name: files.run
  arguments:
    script: mkdir reports
    directory: /srv/workspaces/project
```

Without a mapping, a reviewer sees different target and execution paths. It
should not assume that two similar-looking directories are the same resource.
With admitted runtime context, it can see that the application reports the
logical and execution roots as corresponding:

```yaml
tool_call:
  runtime_context:
    version: 1
    source: example.application
    runtime:
      working_directory: /srv/workspaces/project
      workspace_mappings:
        - logical_root: /workspace/project
          execution_root: /srv/workspaces/project
```

This explains the location without rewriting the model's `target_resource` or
changing the actual tool arguments. It does not require a tool argument named
`cwd`: the application reports the initial working directory explicitly.

## Trust is configured at the Council

Add an issuer to the Council's `council.yaml` only after establishing that the
application validates its runtime facts:

```yaml
require_auth: true
runtime_context:
  trusted_hosts:
    - agent_id: example-agent
      source: example.application
      tools: [files.run]
```

The default trust list is empty; the shipped configuration shows the optional
section as a commented example. Supplying a
`source` string does not establish trust. The service requires all three matches:

| Gate | What must match |
| --- | --- |
| Authenticated account | The request's authenticated agent is `example-agent`; an agent name in the body is not sufficient. |
| Source | The context's `source` is exactly `example.application`. |
| Tool | The structured operation's tool name is exactly `files.run`; wildcards are not accepted. |

The ordinary authentication, Council membership, tool permissions and policy
checks still apply. A supplied but untrusted runtime context is rejected with
`status: error` and `error_code: untrusted_runtime_context`; it is not silently
discarded so review can proceed with fewer facts. Requests without runtime
context retain their existing behavior.

## Core request shape

Runtime context belongs to a structured v2 `tool_call` request. This example
shows the full location-bearing portion of an authorization request; supply the
normal authentication and intent fields as well:

```json
{
  "api_version": "v2",
  "agent_id": "example-agent",
  "council_id": "example-council",
  "target_resource": "/workspace/project/reports",
  "tool_call": {
    "schema_version": 1,
    "tool_name": "files.run",
    "subject": "example-user",
    "arguments": {
      "script": "mkdir reports",
      "directory": "/srv/workspaces/project"
    },
    "runtime_context": {
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
  }
}
```

The core receives only `{version, source, runtime}` as runtime context. When
using the Relay worker, the host's transport assertion additionally contains the
exact tool name, tool-call ID and full business-argument snapshot. The worker
checks those bindings before stripping them from the forwarded context. They
are not additional fields accepted by the core's context model.

The schema requires an initial working directory, workspace mappings, or both.
Paths must be absolute, contain no control characters or dot segments, and fit
the field bounds. There may be at most 16 mappings; duplicate or overlapping
logical roots are rejected. The core validates remote path spelling without
inspecting its own filesystem. The context has a 16 KiB JSON byte limit.

## What reviewers and policy receive

Accepted facts are held separately from model-authored intent text. Council
members see an **Operator-Trusted Host Runtime Resolution** section that explains
their provenance and limits. OPA receives the facts at `input.intent.runtime_context`;
the reported working directory also supplies `input.intent.working_directory`.
If the decision-model stage is enabled, it receives the admitted runtime context
and working directory too, within its normal redaction and request-size checks.

The signed operation still binds the exact actual tool arguments through their
existing digest. Runtime context does not replace those arguments, change their
digest, or become an execution permission. Policy and Council still decide
whether the proposed operation may run.

These are host assertions, not certified executor evidence. They do not prove
that a file exists, that execution occurred, that a person approved anything,
or that a command is confined to its initial working directory. They do not
override a denial or mandatory human approval.

## Privacy and integration limits

The live runtime facts are private review material: the core does not add them
to serialized `Intent` data or the signed operation. This is not an assurance
that paths never enter durable records. Reviewers can quote them in reasons or
guidance, which may be persisted in the review ledger. Treat runtime paths as
information disclosed to the configured reviewers and policy services, and
apply the deployment's logging and data-handling requirements.

Relay transport and worker support are separate integration requirements. Its
complete per-call `execution_context.data` envelope, including the duplicate
argument snapshot and any human-approval assertion, must fit the combined 16 KiB
transport budget. Do not truncate or silently drop context to make it fit. This
core API addition does not by itself configure or activate a Relay worker.
