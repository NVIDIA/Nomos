<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Logs and troubleshooting

The `nomos-request-review` executable writes structured JSON events to stdout
using `tracing`. It does not create or rotate files. Your launcher can capture
stdout or send it to a journal; OpenShell's gateway and supervisor have separate
logs. For example, from this directory:

```sh
cargo run --locked --release --bin nomos-request-review -- > middleware.jsonl 2> middleware.stderr
```

Provide the [model settings](configuration.md#process-settings) first. Treat
logs as operational data: although request bodies and credentials are omitted,
identifiers, hashes, scores and configured model names can be sensitive.

## Events

| `fields.message` | Meaning |
| --- | --- |
| `isolated_lab_no_inbound_authentication` | Startup warning with listener and timeout budgets; emitted before binding, so not proof the listener started |
| `review_input_validated` | Policy and HTTP input validated; records catalog, question count and original-body SHA-256 |
| `model_call_started` | Complete model request serialized; records byte count and SHA-256 before HTTP send |
| `model_call_finished` | Provider call exited, including errors or cancellation |
| `review_complete` | Valid answers produced an allow or deny decision |
| `review_failed` | Evaluation error, including input rejection, capacity exhaustion or timeout |
| `authenticated_middleware_listener` | Startup with extension authentication/native TLS configured |
| `context_lookup_audit` | Metadata-only lookup completed; no content added to the model |
| `context_lookup_failed` | Sanitized lookup failure; required mode fails, best-effort/audit continue HTTP-only |
| `context_budget_complete` | Included/omitted returned-record counts and a fixed budgeting reason |
| `context_http_only_reassessment` | Enriched input received an explicit size rejection; one unchanged HTTP-only assessment follows |

With context enabled, lookup/budget events also appear before model assessment.
They contain no user text, tool arguments, Council guidance, response bodies or tokens.
See [context review](context-review.md) for mode and budget semantics.

A normal sequence is input validated → model call started → model call finished
→ review complete. Invalid input can emit only `review_failed`. A provider
failure normally ends with `model_call_finished` then `review_failed`.
Oversized transport messages rejected before the handler do not produce an
evaluation event. Caller cancellation can produce `model_call_finished` without
a final review event.

## Correlate a request

OpenShell supplies `request_id` and `sandbox_id`. They identify a request and
sandbox for correlation, not a verified tool call or conversation. IDs appear
in model events' `span` object (and possibly `spans`), and in completed/failed
review events' `fields`. Missing or invalid IDs are omitted from failure logs.
Search both locations when joining events.

These formatted examples show selected fields. Actual records occupy one line;
values are illustrative, not measurements:

```json
{
  "level": "INFO",
  "fields": {
    "message": "model_call_finished",
    "model_request_bytes": 1200,
    "model_response_bytes": 100,
    "model_wait_ms": 245.2,
    "model_completion_observed": true,
    "model_outcome": "complete"
  },
  "span": {"name": "review", "request_id": "request-example", "sandbox_id": "sandbox-example"}
}
```

```json
{
  "level": "INFO",
  "fields": {
    "message": "review_complete",
    "request_id": "request-example",
    "sandbox_id": "sandbox-example",
    "catalog": "request-review-v1",
    "model": "example-model-v1",
    "outcome": "deny",
    "model_ms": 246,
    "total_ms": 247
  }
}
```

`review_complete` also records the original yes scores and matched rule IDs.
Those fields currently use debug-formatted strings rather than nested JSON
objects/arrays. An allow has no matching deny rule. Neither an allow nor a deny
event alone proves whether the destination received anything; inspect the
destination and OpenShell enforcement logs too.

```json
{
  "level": "WARN",
  "fields": {
    "message": "review_failed",
    "request_id": "request-example",
    "sandbox_id": "sandbox-example",
    "code": "Unavailable",
    "total_ms": 4001
  }
}
```

`InvalidArgument` indicates input/configuration rejection. `ResourceExhausted`
can mean body/context limits or exhausted evaluation capacity. `Unavailable`
includes model HTTP/transport failures and invalid assessments;
`DeadlineExceeded` indicates the whole evaluation budget expired.

## Read model timing and outcome

`model_call_finished` does not necessarily mean the model returned valid answers:

| `model_outcome` | Meaning |
| --- | --- |
| `complete` | Response and model identity passed validation |
| `timeout` | Model HTTP request/response budget expired |
| `transport_error` | HTTP connection or response-stream failure |
| `http_error` | Non-success status without a recognized size failure |
| `input_over_limit` | HTTP 413 or recognized token-context error |
| `invalid_assessment` | Malformed, oversized or invalid answer, including identity mismatch |
| `cancelled` | Provider future dropped; does not identify which caller/deadline caused it |

`model_completion_observed` means the full response body was read within its
bound; it can be true for an error response or an invalid assessment. HTTP 413
is classified without reading its body. `model_response_bytes` counts chunks
read, including a chunk that exceeds the response cap.

`model_wait_ms` measures local provider wait through HTTP I/O, body parsing and
answer validation. It excludes earlier request serialization/hashing.
`model_ms` spans the whole provider call; `total_ms` spans handler work. These
are not pure inference timings or direct measurements of network latency.

Optional usage fields are recorded only when supplied as supported nonnegative
integer counters by the endpoint. `usage_schema` identifies the source field
names; missing values remain unknown. `output_tokens_basis`, including the
possible value `scored_decision_positions`, is copied from the endpoint rather
than inferred by this middleware. Scoring positions are not necessarily
generated text tokens or billable units. Server total/engine/prepare times are
endpoint-reported, may overlap and must not be summed. These logs do not by
themselves establish cost.

## Data boundaries

Logs omit bodies, queries, header values, questions, API tokens and raw provider
responses/errors. They retain hashes, correlation IDs, model/catalog names,
scores and counters. The body hash covers original HTTP body bytes; the model
request hash covers the complete serialized state, model and questions. Hashes
can still reveal repeated or guessable content; apply appropriate access and
retention controls.

An `Unavailable` event alone cannot distinguish a bad credential, wrong model
identity or timeout. Correlate the provider outcome and inspect the configured
endpoint/model settings. Do not disable response identity validation to make a
request pass. See [source events](../src/service.rs),
[provider telemetry](../src/provider.rs), and [timeout settings](configuration.md#three-timeout-budgets).
