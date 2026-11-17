<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Host-verified human approval: local prototype

This optional path lets an operator trust an application to finish its human
approval checks before asking the plugin to dispatch a local tool. Ordinary
guarded calls still require the Council's authorization. The feature is disabled
when `host_approval` is omitted.

## Compatibility and release status

This is an opt-in prototype, not a released installation path. The required
transport is proposed in [NeMo Relay PR #1749](https://github.com/NVIDIA/NeMo-Relay/pull/1749),
which is still a draft and is not a published SDK release. The tracked package,
lockfile and manifest continue to describe the existing Relay 0.11.1 distribution.
That SDK cannot carry execution context; enabling this feature with it fails at
activation. The prototype requires a matching source-built Relay host, binding
and worker SDK with `ToolExecutionContext.execution_context`, the negotiated
`tool-execution-context-v1` capability, and the newer
`nvidia.nemo.relay.worker.v1` protocol.

Local testing uses a separately generated packaging/manifest overlay and isolated
environment. No machine-specific overlay paths are part of the repository. A
publicly resolvable SDK release, package/lockfile migration and the corresponding
manifest migration are required before releasing this feature. A changed manifest
alone cannot make an old worker understand a new protocol.

## What the operator trusts

Enable only exact guarded tools with `executor = "local"`:

```toml
[plugins.dynamic.config.host_approval]
source = "example.application"
tools = ["write_file"]
```

The application is responsible for authenticating the approver, checking that
the approver may approve this operation, verifying the signed or otherwise trusted
approval record, checking its current state and expiry, and binding the approval
to the exact final execution payload. It must repeat relevant checks immediately
before the actual business callback. The worker's configured `tool_call.subject`
provides an operator-owned journal scope; this payload does not map an application
user to that subject or independently authenticate the approver.

The source string is an allowlist identifier, not a secret credential. Relay
transports caller-supplied data and does not prove that a person approved it.
The feature does not protect against a compromised trusted application or another
trusted host extension that can manufacture the same assertion.

## Per-call payload

The host supplies this object through Relay's separate `execution_context`
parameter. It must not come from model arguments, prompt text or event metadata:

```json
{
  "host_approval": {
    "version": 1,
    "source": "example.application",
    "decision": "approved",
    "approval_id": "approval-example",
    "tool_name": "write_file",
    "approved_arguments": {
      "path": "/workspace/project/note.txt",
      "content": "hello"
    }
  }
}
```

The outer object and six assertion fields are closed: unexpected fields fail
validation. Approval IDs must be nonblank and at most 256 characters. The complete
execution context must fit Relay's 16 KiB and 64-container-level bounds; the host must refuse an oversized
assertion rather than truncate it or silently omit it.

`approved_arguments` must equal the actual held business arguments after removing
only the top-level `nomos` review field. It includes all other JSON fields,
including final working-directory, workspace-root and other routing fields when
present. The comparison is structural: object-key order is immaterial, array
order matters, strings differ from numbers, and booleans differ from numbers.
`1` and `1.0` represent the same JSON number. Nonfinite numbers are rejected. This
avoids imposing a second, incompatible cross-language hashing format.

## Execution and replay behavior

For a valid trusted assertion, the plugin skips its own model-intent, native
request-origin, OPA/Council and Council-writ checks. It calls the local continuation
with the exact held business payload. It returns the ordinary `nomos-plugin/v1`
envelope and preserves the tool's annotation, with
`authorization_source = "host_human_approval"`. It does not invent a Council
decision or writ. Results on this path are not added to the Council-certified
evidence catalog.

Missing assertions leave the ordinary Council path unchanged. Invalid or untrusted
assertions fail closed; they do not authorize execution or trigger a fallback.
Putting an assertion in model tool arguments grants nothing.

A separate journal is scoped to the configured source and subject and keyed by
approval ID. A repeated identical approval returns `host_approval_replayed` with
`execution_status = "not_executed"`; it does not invoke the tool again or pretend
that this invocation executed a cached result. Reusing an ID with a changed held
payload is a conflict. A cancellation or uncertain callback outcome prevents
automatic retry and requires reconciliation.

The journal is bounded by the existing retention and quota settings and lives
only for the worker activation. It is not durable or distributed exactly-once
execution. The application's durable approval consumption and effect ledger
remain necessary across worker restarts and retention expiry.

Authorization runs at priority 90, before Relay's default cache priority 150.
Operators must not place caches before it or allow downstream middleware to
rewrite guarded business arguments. Consequential tools should not be answered
from a result cache. The host's final callback must still verify the exact payload.

## Validation

Focused tests cover disabled trust, SDK compatibility, malformed and spoofed
assertions, exact argument matching, ordinary Council denial, replay, cancellation
and result annotations. An opt-in regression enables the real Relay tool cache
and verifies that a cached answer cannot bypass a rejected assertion:

```sh
NOMOS_TEST_SOURCE_RELAY=1 python -m pytest -q tests/test_host_approval_runtime.py
```

Run that test in the isolated source-matched prototype environment. Existing
installed-worker and live-service tiers remain separate opt-in checks.

Unit tests simulate the new callback field while retaining the published SDK
pins. This does not demonstrate transport support in SDK 0.11.1. Activation
explicitly rejects configured host approval when that SDK lacks the field.
The opt-in runtime tier requires the matching source-built host and worker SDK;
it is not included in claims about the locked-package test suite.
