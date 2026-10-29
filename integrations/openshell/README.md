<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# OpenShell request-review middleware (experimental)

This Rust service lets an OpenShell administrator ask a decision model to review
outgoing HTTP request content. OpenShell first applies its network policy, then
calls this service for traffic selected by the middleware policy. The service
returns an allow or deny decision; **it does not replace OpenShell's network
policy, forward traffic, or grant additional network access**.

The service implements OpenShell's `EvaluateHttpRequest` RPC at
`PRE_CREDENTIALS`. It accepts UTF-8 JSON objects and supports different HTTP
methods and routes. It currently returns no request mutations and implements no
response or WebSocket inspection. Plain text, multipart uploads and compressed
bodies are not supported at this time.

This is separate from Nomos's [OpenShell executor](../../executors/openshell/).
It does not call Nomos Council or receive conversation history or tool-call
context. OpenShell request/sandbox IDs are used only for log correlation.

**Deployment limitation:** inbound gRPC has no TLS or caller authentication.
Keep the listener on loopback or a strictly isolated, trusted development network.
The explicit `--allow-insecure-lab` flag is required for a nonloopback listener;
it does not add authentication. Do not expose this listener publicly. This is
an experimental integration, not an authenticated production deployment.

## Read and run

1. [Setup](docs/setup.md): build, configure a model, register with OpenShell,
   and select traffic in sandbox policy. For a complete local example, follow
   [the integration lab](docs/lab-setup.md), including its capture server and
   Ubuntu 24.04 curl sandbox.
2. [Configuration](docs/configuration.md): questions, denial direction, timeouts
   and the complete decision-model API contract with formatted examples.
3. [Payload limits](docs/payload-limits.md): byte gates, sizing rationale and
   upstream context-limit errors.
4. [Logging](docs/logging.md): events, correlation and troubleshooting.
5. [Testing](docs/testing.md): local transport tests and manual validation.
6. [Lab testing](docs/lab-testing.md): deterministic sandbox checks with the
   simulated model, route/policy controls and real-model smoke examples.

From this directory, with Rust 1.95.0 available:

```sh
cargo build --locked --release
```

Both the endpoint and model ID must be configured explicitly. The compatible
model API and its expected response are described in [configuration](docs/configuration.md#model-api).
No model or model weights are included.

```sh
export NOMOS_MODEL_ENDPOINT='https://decision.example.com/v1/systemone'
export NOMOS_MODEL_ID='example-model'
# Set NOMOS_MODEL_TOKEN securely if the endpoint requires a bearer credential.
cargo run --locked --bin nomos-request-review
```

The endpoint and model above are placeholders; replace them with your own
compatible service. The default gRPC listener is `127.0.0.1:50051`.

## Dependencies and protocol compatibility

Builds download additional third-party open-source software. Review its terms
before use; the complete Cargo.lock inventory is in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). The optional
[Python test destination](tools/test-destination/README.md) has separate locked
dependencies and is used only for integration validation. Benchmark datasets,
runners and reports are not included.
The [integration lab helpers](tools/integration-lab/README.md) use only Python's
standard library and do not require model credentials for simulated checks.

Bindings are generated from the copied Apache-2.0 OpenShell protocol files at
the [documented public revision](proto/UPSTREAM.md). Use a compatible OpenShell
build and check protocol negotiation before sending traffic. This integration
does not promise compatibility with every OpenShell release.
