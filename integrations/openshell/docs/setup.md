<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Set up the request-review service

You need Rust 1.95.0, a compatible decision-model HTTP API, and an OpenShell
installation supporting the [pinned supervisor middleware contract](../proto/UPSTREAM.md).
Use the public [OpenShell installation and setup guide](https://github.com/NVIDIA/OpenShell)
for the gateway and sandbox runtime. This guide starts with an installed
OpenShell deployment. For the complete Docker bridge example, including a
curl workload image and test destination, follow [the integration lab](lab-setup.md).

## 1. Build and start the service

```sh
git clone --branch develop https://github.com/NVIDIA/Nomos.git
cd Nomos/integrations/openshell
rustup toolchain install 1.95.0 --profile minimal --component rustfmt --component clippy
cargo build --locked --release
```

Choose an endpoint that implements the [model API](configuration.md#model-api).
Supply these process settings, rather than embedding credentials or endpoint
selection in sandbox policy:

```sh
export NOMOS_MODEL_ENDPOINT='https://decision.example.com/v1/systemone'
export NOMOS_MODEL_ID='example-model'
# If the server returns a different exact model identity:
export NOMOS_EXPECTED_MODEL_ID='example-model-v1'
# Set NOMOS_MODEL_TOKEN through your environment or secret manager, if required.
cargo run --locked --release --bin nomos-request-review --
```

All three values above are illustrative. Use your endpoint's actual request
alias and response identity. If they are identical, omit
`NOMOS_EXPECTED_MODEL_ID`. HTTPS certificate and hostname verification remain
enabled, using the machine's trusted roots. Redirects and automatic retries are
disabled. For a local HTTP model fixture only, add `--allow-http-model`.

## 2. Make the listener reachable by OpenShell

Registration connects to a running service; it does not start it. Both the
gateway and sandbox supervisors must be able to reach its gRPC address. The
loopback default works only when the caller shares that network namespace.

The [gateway example](../examples/gateway.toml) uses
`http://host.openshell.internal:50051`. That alias depends on the OpenShell
driver and deployment. Verify reachability from both callers and replace the
address when needed. A Docker bridge caller cannot reach a host listener bound
only to `127.0.0.1` through the host alias.

For a strictly isolated development network, bind to an appropriate reachable
interface, for example:

```sh
cargo run --locked --release --bin nomos-request-review -- \
  --bind 0.0.0.0:50051 --allow-insecure-lab
```

Restrict access with the deployment's network controls. The service does not
authenticate inbound calls or terminate inbound TLS; `allow_insecure_transport`
in the example registration explicitly acknowledges plaintext transport.

## 3. Register and select traffic

Merge [examples/gateway.toml](../examples/gateway.toml) into the gateway
configuration that your OpenShell deployment actually loads:

```toml
[[openshell.supervisor.middleware]]
name = "nomos-request-review"
grpc_endpoint = "http://host.openshell.internal:50051"
allow_insecure_transport = true
max_payload_bytes = 262144
timeout = "5s"
```

Follow your OpenShell deployment's configuration/restart procedure. Do not
assume OpenShell reads an arbitrary file automatically just because it is named
`gateway.toml`.

Use [examples/policy.yaml](../examples/policy.yaml) as a sandbox-policy example.
It selects the registered middleware, sets `on_error: fail_closed`, supplies two
questions, and permits `/usr/bin/curl` to call an illustrative JSON destination
at `host.openshell.internal:18081/publish`. Run the included
[test destination](../tools/test-destination/README.md) separately, following
[the lab guide](lab-setup.md), or replace it with a destination you control.

The example allows any HTTP method on that route. OpenShell's network policy
still determines allowed hosts, ports, executables, methods and paths. The
middleware's endpoint selector determines which eligible traffic receives
content review. Review both sections when adapting the example; permitting a
destination alone does not establish the intended middleware coverage.

Create or update a sandbox using your OpenShell version's policy workflow. An
already running sandbox does not necessarily pick up edits to example files.
Verify the service's `Describe` handshake and `ValidateConfig` result before
performing [manual tests](testing.md#manual-openshell-validation).

## 4. Check the deployment

Watch middleware stdout and OpenShell's gateway/supervisor logs. Send one
synthetic benign request, one synthetic disclosure and one malformed body.
Confirm both the reviewer outcome and destination arrival: an allow decision
alone does not prove forwarding. Keep test destinations and model endpoints
under your control and use no real credentials in request bodies.

See [logging](logging.md) for event meanings and [payload limits](payload-limits.md)
for the coordinated 256 KiB body and 512 KiB gRPC defaults.
