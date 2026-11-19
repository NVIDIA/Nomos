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
which is not yet a published SDK release. The tracked package,
lockfile and manifest continue to describe the existing Relay 0.11.1 distribution.
That SDK cannot carry execution context; enabling this feature with it fails at
activation. The prototype requires a matching source-built Relay host, binding
and worker SDK implementing `ToolExecutionContext.execution_context` as part of
the required `nvidia.nemo.relay.worker.v1@1.0.0` contract. No separate worker
capability advertisement is required. Rebuild and repackage development workers
with the matching SDK; a stale development binary reporting the same contract
version cannot be identified by its version alone.

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
  "data": {
    "host_approval": {
      "version": 1,
      "source": "example.application",
      "approval_id": "approval-example",
      "tool_name": "write_file",
      "approved_arguments": {
        "path": "/workspace/project/note.txt",
        "content": "hello"
      }
    }
  }
}
```

Relay defines a closed `ApplicationExecutionContext` envelope with an optional
`data` object. The envelope accepts only `data`; its contents are opaque to Relay.
**Nomos owns the approval contract** at `data.host_approval`, defined by the
packaged [host-approval.schema.json](../nemo_relay_nomos/host-approval.schema.json)
and enforced by the [approval validator](../nemo_relay_nomos/host_approval.py).
The schema describes the five required fields and permits additional assertion
properties. Other application data may coexist alongside `data.host_approval`.
Extensions never override the trusted source, exact tool or approved arguments.

A valid claim always asserts completed affirmative human approval. The obsolete
`decision` property is reserved and rejected regardless of value, so a denial
cannot become an ignored extension on an approval. Use namespaced extension keys
to reduce collisions. Approval IDs must be nonblank and at most 256 characters.
The complete execution context must fit Relay's 16 KiB and 64-container-level
bounds; the host must refuse an oversized assertion rather than truncate it or
silently omit it. Schema validation establishes shape, not trust: the operational
checks below remain necessary.

`approved_arguments` must equal the actual held business arguments after removing
only the top-level `nomos` review field. It includes all other JSON fields,
including final working-directory, workspace-root and other routing fields when
present. The comparison is structural: object-key order is immaterial, array
order matters, strings differ from numbers, and booleans differ from numbers.
`1` and `1.0` represent the same JSON number. Nonfinite numbers are rejected. This
avoids imposing a second, incompatible cross-language hashing format.

## Execution and replay behavior

For a valid trusted assertion, the plugin skips model-intent and native
request-origin correlation, then asks the authenticated Council service to
inspect **current OPA policy** for the exact structured tool and arguments using
`policy_only = true`. This inspection does not call Council members or mint a
writ. It requires the new core policy-outcome API and enabled, healthy OPA;
an older service, disabled policy, malformed response or unavailable service
blocks execution. Ordinary authorization's fail-open setting cannot authorize
this replay.

An explicit `policy_allowed` or `requires_human_approval` response status can proceed,
because the worker has already verified the host's exact human-approval claim.
A `denied` status still blocks the call: human approval cannot
override central policy. Operator trust is checked again after inspection in
case it expired while waiting. The host must still revalidate its own saved
approval immediately before execution.

After these checks, the plugin calls the local continuation with the exact held
business payload. It returns the ordinary `nomos-plugin/v1`
envelope and preserves the tool's annotation, with
`authorization_source = "host_human_approval"`. It does not invent a Council
decision or writ. Results on this path are not added to the Council-certified
evidence catalog.

An absent context, absent `data`, or valid `data` without `host_approval` leaves
the ordinary Council path unchanged. A present null or malformed claim, malformed
envelope, or legacy flat `execution_context.host_approval` fails closed with
`host_approval_invalid`; it never falls back to ordinary Council review. Untrusted
assertions also fail closed.
Putting an assertion in model tool arguments grants nothing.

A separate journal is scoped to the configured source and subject and keyed by
approval ID. A repeated identical approval returns `host_approval_replayed` with
`execution_status = "not_executed"`; it does not invoke the tool again or pretend
that this invocation executed a cached result. Reusing an ID with a changed held
payload is a conflict. A cancellation or uncertain callback outcome prevents
automatic retry and requires reconciliation.

A policy failure also closes that journal attempt without dispatch. Retrying
requires a fresh host approval ID; the worker does not retry automatically or
turn a service outage into a request for human approval.

## Decisions before human approval

For an ordinary guarded proposal, OPA runs before Council review. Its explicit
`requires_human_approval` status returns the following envelope without calling
the continuation:

```json
{
  "contract_version": "nomos-plugin/v1",
  "status": "requires_human_approval",
  "execution_status": "not_executed",
  "evidence_status": "unavailable",
  "policy": {
    "reason": "The command needs human review.",
    "guidance": [],
    "decision_id": "example-policy-decision"
  }
}
```

Signed-record fields may also be present when evidence recording is enabled.
The application can use this explicit outcome to create its existing approval
card and later submit the exact saved operation with a host assertion. A policy
prohibition or Council refusal returns `status = "denied"` and keeps the existing
bounded denial guidance. There is no second public policy-decision field: the
top-level status alone distinguishes denial from required human approval.

A human-review requirement is not a Council vote and does not increment the
denial counter. Unified `denied` responses from the Council service count toward
the existing consecutive authorization-denial limit, whether the service denied
at policy or model review. Their existing signed service-response evidence is
preserved; it does not manufacture member votes. Errors never become
human-approval requests.

Only a non-executing `requires_human_approval` response can request a card.
Responses carrying a writ, writ identifier or contradictory execution outcome
are rejected. Old `denied` responses with an auxiliary human-review claim stay
denied; the worker does not upgrade them by inspecting legacy fields or prose.
New inspection/human-review statuses mixed with the obsolete auxiliary field
are malformed and fail closed. Deploy the updated core status contract first,
then update the worker and consuming application before enabling this draft
feature; a mixed-version deployment may refuse operations rather than silently
selecting a different review route.

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

For the combined policy contract, use a core checkout containing the structured
tool-policy API and an OPA binary, in that same isolated source-Relay environment:

```sh
export NOMOS_TEST_CORE_POLICY=1
export NOMOS_TEST_NOMOS_SOURCE=/path/to/nomos-policy-checkout
export NOMOS_TEST_OPA_BIN=/path/to/opa
python -m pytest -q tests/test_core_policy_runtime.py
```

This tier runs the real core service and loopback OPA, and passes execution
context through the real Relay runtime. Callback effects are synthetic: no
shell command, model inference or live connector call runs. It covers ordinary
human-required/forbidden decisions, policy-allowed approved replay, a satisfied
human-review requirement, and a hard denial that still blocks approved replay.
Middleware runs in process; this does not assert remote worker-protocol coverage.
