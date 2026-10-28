<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Test the middleware

## Automated local checks

From `integrations/openshell`:

```sh
cargo fmt --all -- --check
cargo test --locked --all-targets
cargo clippy --locked --all-targets -- -D warnings
RUSTDOCFLAGS='-D warnings' cargo doc --locked --no-deps
```

The default test run requires no API token, running OpenShell deployment or external model.
Cargo downloads build dependencies on a first build. Tests that exercise
transport bind local ephemeral HTTP/gRPC listeners only.
The optional [live connectivity test](../tests/live.rs) is ignored by default;
running it explicitly contacts your configured model. See
[live lab checks](lab-testing.md#check-your-actual-model-separately).

| Check | Where | What it verifies |
| --- | --- | --- |
| Service/transport boundaries | [tests/review.rs](../tests/review.rs) | Actual local HTTP and gRPC calls, protocol negotiation, request validation, threshold decisions, error mapping and log sanitization |
| Strict JSON parsing | [src/strict_json.rs](../src/strict_json.rs), exercised by [tests/review.rs](../tests/review.rs) | Duplicate-key rejection and parser bounds |
| Startup arguments | [src/main.rs](../src/main.rs) and [tests/cli.rs](../tests/cli.rs) | Required model selection and configurable byte-limit parsing |
| Timeout validation | [src/timeouts.rs](../src/timeouts.rs) | Finite, ordered model/evaluation budgets |

Some service tests provide an in-memory `DecisionProvider` that returns a fixed
assessment, so the service's threshold/error behavior is deterministic. Other
tests start a temporary local HTTP server that returns prepared model responses,
then exercise the real `SystemOneProvider` HTTP adapter. Neither kind evaluates
model quality; they verify how the service handles controlled answers and failures.

Examples include missing or unexpected answers, out-of-range scores, wrong model
identity, invalid JSON, duplicate keys, content encodings, concurrency rejection,
oversized body/transport/response messages, deadlines and upstream context errors.
Tests also verify body preservation and catalogs larger than eight questions.

## Destination server checks

The optional Python destination has independent tests for raw-byte preservation,
arrival counts, method/path evidence, bounded capture and cursor inspection.
From `integrations/openshell`:

```sh
uv run --locked --project tools/test-destination pytest tools/test-destination/tests
```

See [the destination tests](../tools/test-destination/tests/test_server.py) and
[its guide](../tools/test-destination/README.md). These tests do not call a model.

## Manual OpenShell validation

For scripted sandbox checks and preset model modes, follow
[lab testing](lab-testing.md) and the
[integration helper guide](../tools/integration-lab/README.md). These helpers
exercise the actual OpenShell path; their own unit tests run independently:

```sh
python3 -m unittest discover -s tools/integration-lab -v
```

Follow [the complete lab guide](lab-setup.md) to run the included
[test destination](../tools/test-destination/README.md), or use the
[setup guide](setup.md) with another destination you control that records arrivals. Update the policy's host, port, path and executable to match it. The
following requests assume the example route and a working `curl` in the sandbox:

```sh
curl --silent --show-error --include --max-time 10 \
  http://host.openshell.internal:18081/publish \
  --header 'content-type: application/json' \
  --data-binary '{"message":"The community workshop starts tomorrow."}'

curl --silent --show-error --include --max-time 10 \
  http://host.openshell.internal:18081/publish \
  --header 'content-type: application/json' \
  --data-binary '{"message":"Internal confidential plan: FictionalCo will acquire ExampleCo next week."}'

curl --silent --show-error --include --max-time 10 \
  http://host.openshell.internal:18081/publish \
  --header 'content-type: application/json' \
  --data-binary '{"message":"first","message":"second"}'
```

All content is synthetic. The first example should allow and the second should
deny under the example disclosure questions, subject to the model's assessment.
The third is rejected by this middleware's duplicate-key parser before a model
call. An invalid input is an assessment failure, not a scored harmful-content
decision. OpenShell chooses the workload's HTTP error response under fail-closed
policy.

For each request, correlate OpenShell and [middleware logs](logging.md) and check
destination arrivals. Expect `review_complete` for valid model answers and
`review_failed` for malformed input. Also test a deliberately stopped service
and confirm fail-closed behavior. A connection failure before the middleware
handler runs will not produce a review event in that process.

These steps validate integration behavior. They do not establish model accuracy,
token-window boundaries, latency guarantees or production readiness.
