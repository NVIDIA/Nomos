<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Run an isolated OpenShell integration lab

This walkthrough connects a curl sandbox to the request-review middleware and
the included test destination. It assumes an Ubuntu/Debian Linux development
machine with Docker already installed and usable by your account. You also
need Git, curl, compiler/linker tools (`build-essential` on Ubuntu),
[Rust/rustup](https://rustup.rs/) and [uv](https://docs.astral.sh/uv/getting-started/installation/).

Use fictional content only. The destination and middleware have unauthenticated
lab listeners; the destination exposes captured bodies through `/requests`.
Keep their ports restricted to the development host and trusted bridge network.
The gateway uses its normal package-managed HTTPS configuration.

## 1. Clone and choose a compatible OpenShell build

```sh
git clone --branch develop https://github.com/NVIDIA/Nomos.git
cd Nomos/integrations/openshell
```

Run subsequent repository commands from this directory, including commands in
additional terminals.

Install OpenShell using its public [installation instructions](https://github.com/NVIDIA/OpenShell/blob/main/docs/about/installation.mdx)
and [published releases](https://github.com/NVIDIA/OpenShell/releases).
You need the CLI and gateway, Docker bridge supervisor support, schema-v2 gateway
configuration, and a middleware protocol compatible with
[our copied contract](../proto/UPSTREAM.md). Keep CLI, gateway, supervisor and
sandbox-runtime artifacts from the same OpenShell build. Let the gateway select
its matching published runtime images; no custom supervisor build is needed.

For a fresh development machine, the public rolling development installer is:

```sh
curl -LsSf https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh | \
  OPENSHELL_VERSION=dev sh
```

On Ubuntu/Debian it installs the package and starts the gateway's user service.
Use an exact compatible release tag in place of `dev` when qualifying a fixed
release. The installer starts the initial gateway before Nomos registration;
step 6 restarts it after the custom lab configuration is ready.

```sh
openshell --version
openshell-gateway --version
systemctl --user daemon-reload
```

Record the installed versions and package checksum when reproducing a run.
Rolling `dev` downloads can change; a release label alone does not pin an artifact.
This guide describes the bridge configuration and package-managed HTTPS flow;
the local server tests do not certify that flow on every OpenShell release.
Run preflight and the validation steps below on your actual installation.

## 2. Build the middleware and Ubuntu 24.04 curl image

```sh
rustup toolchain install 1.95.0 --profile minimal --component rustfmt --component clippy
cargo build --locked --bin nomos-request-review
cargo test --locked --all-targets
uv run --locked --project tools/test-destination pytest tools/test-destination/tests
docker build -t nomos-review-workload:lab examples/curl-sandbox
```

The [Dockerfile](../examples/curl-sandbox/Dockerfile) adds `/usr/bin/curl` to the
public [NVIDIA Ubuntu **24.04** base image](https://catalog.ngc.nvidia.com/orgs/nvidia/base/containers/ubuntu/24.04),
with the original base-image digest retained. Anonymous pull access is available. It builds
only workload tools and runs as the base image's unprivileged `ubuntu` account
(UID/GID 1000). Package installation downloads upstream Ubuntu packages;
those packages retain their own licenses. Build it with the same Docker daemon
used by the gateway. No image or model weights are distributed by this example.

## 3. Check bridge reachability

The supervisor must reach three host listeners: the gateway on 17670, Nomos on
50051 and the test destination on 18081. The host gateway process must also
reach Nomos. Loopback inside a supervisor container refers to that container.

```sh
BRIDGE_IP="$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')"
printf '%s\n' "$BRIDGE_IP"
getent hosts host.openshell.internal
```

Use the bridge's IPv4 gateway address for this example. OpenShell's Docker
driver supplies the host alias inside bridge supervisors. The host needs to
resolve the same alias too. If the host lookup is absent, add it:

```sh
printf '%s host.openshell.internal\n' "$BRIDGE_IP" | sudo tee -a /etc/hosts
```

If an entry already exists, check/correct it rather than appending a duplicate.
Different Docker setups may require a different reachable address; confirm
resolution and routing before continuing. Do not change unrelated host entries.

## 4. Start the destination — terminal A

```sh
BRIDGE_IP="$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')"
uv run --locked --no-dev --project tools/test-destination \
  test-destination --host "$BRIDGE_IP" --port 18081
```

Leave it running. Recompute the address in each new host terminal before using it:

```sh
BRIDGE_IP="$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')"
curl --fail "http://$BRIDGE_IP:18081/health"
curl --fail "http://$BRIDGE_IP:18081/requests"
```

The [destination guide](../tools/test-destination/README.md) explains raw-byte
records and limits. Restarting clears the memory-only records. It does not call
the model and should receive no direct host-side publications during validation.

## 5. Start the middleware — terminal B

Choose one of the following modes. The simulated model lets you validate the
complete integration without an external model account or credential.

### Option A: local simulated model

In an additional terminal, start the fixture:

```sh
python3 tools/integration-lab/mock_decision_model.py --threshold 0.7 --delay 6
```

Then start the middleware in terminal B, recomputing its bridge bind address:

```sh
BRIDGE_IP="$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')"
env -u NOMOS_MODEL_TOKEN cargo run --locked --bin nomos-request-review -- \
  --model-endpoint http://127.0.0.1:18082/v1/systemone \
  --model-id nomos-lab-fixture --expected-model-id nomos-lab-fixture \
  --allow-http-model --bind "$BRIDGE_IP:50051" --allow-insecure-lab
```

The fixture returns preset scores without analyzing content. Only the middleware
calls it; the sandbox still sends publications to the destination. Its initial
low scores allow both benign and confidential content, so use the
[deterministic helper checks](lab-testing.md#run-the-default-checks) to change
fixture modes and verify the expected decisions. `env -u` prevents passing a
real endpoint credential to this local fixture.

### Option B: your own model endpoint

Instead, configure your own compatible decision-model endpoint. The following
values are placeholders, not an included model service:

```sh
BRIDGE_IP="$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')"
export NOMOS_MODEL_ENDPOINT='https://decision.example.com/v1/systemone'
export NOMOS_MODEL_ID='example-model'
# Set NOMOS_EXPECTED_MODEL_ID if the exact returned name differs from the alias.
# Set NOMOS_MODEL_TOKEN securely if your endpoint requires a bearer credential.
cargo run --locked --bin nomos-request-review -- \
  --bind "$BRIDGE_IP:50051" --allow-insecure-lab
```

Replace both placeholder values. See the [complete model API example](configuration.md#model-api)
for required request and answer shapes. HTTPS verification to the model remains
enabled. `--allow-insecure-lab` permits unauthenticated inbound gRPC on a
nonloopback address; it does not disable model HTTPS verification or gateway TLS.
Leave the process running and watch its [structured logs](logging.md).
Both lab listeners bind only to the selected bridge address, rather than LAN
or VPN addresses. This is not authentication: retain firewall restrictions
and trust only the host and intended bridge clients.

## 6. Configure the package-managed gateway — terminal C

Generate a candidate configuration using the bridge address from step 3:

```sh
BRIDGE_IP="$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')"
LAB_GATEWAY_CONFIG="$(mktemp --suffix=.toml)"
sed "s/BRIDGE_ADDRESS/$BRIDGE_IP/g" examples/lab-gateway.toml > "$LAB_GATEWAY_CONFIG"
openshell-gateway config preflight --path "$LAB_GATEWAY_CONFIG"
```

The [template](../examples/lab-gateway.toml) sets:

- The gateway bind address to the bridge address on 17670.
- `supervisor_network_mode = "bridge"` explicitly.
- The supervisor's gateway callback to `https://host.openshell.internal:17670`.
- The local workload image and `image_pull_policy = "never"`.
- Nomos at `http://host.openshell.internal:50051`, body cap 256 KiB and RPC budget 5 s.

The two `grpc_endpoint` settings are different connections. The Docker driver's
endpoint reaches the HTTPS gateway; the middleware registration reaches Nomos's
plaintext lab listener. The workload's network access remains mediated by
OpenShell; the workload is not given unrestricted Docker bridge egress.

For a **fresh lab configuration**, install the candidate without overwriting an
existing file:

```sh
OPENSHELL_CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/openshell"
mkdir -p "$OPENSHELL_CONFIG_DIR"
cp --no-clobber "$LAB_GATEWAY_CONFIG" "$OPENSHELL_CONFIG_DIR/gateway.toml"
```

If that file already exists, the copy leaves it unchanged: merge the template
settings into it yourself before proceeding. Review the effective file and
preflight it. With default XDG settings, the user service reads
`~/.config/openshell/gateway.toml`; a configured `OPENSHELL_GATEWAY_CONFIG` or
process override can select another file. See the public
[gateway configuration reference](https://github.com/NVIDIA/OpenShell/blob/main/docs/how-it-works/gateways/configuration.mdx).
The package initializes its local certificates and signing keys; no manual key
generation or TLS-directory override is needed here.

Restarting the user service affects any other workloads using that gateway;
use a dedicated lab installation. With terminals A and B still running:

```sh
openshell-gateway config preflight --path "$OPENSHELL_CONFIG_DIR/gateway.toml"
systemctl --user enable openshell-gateway
systemctl --user restart openshell-gateway
systemctl --user status openshell-gateway --no-pager
openshell gateway add https://host.openshell.internal:17670 --local --name review-lab
openshell gateway select review-lab
openshell gateway list
openshell status
```

The restart loads server settings. `gateway add` registers a CLI connection
alias; it does not start another gateway. `gateway select` makes it the default,
so later commands need no gateway flag. Check/unset conflicting
`OPENSHELL_GATEWAY` or `OPENSHELL_GATEWAY_ENDPOINT` overrides. Registration and
default selection belong to each Unix account. Inspect failures with:

```sh
journalctl --user -u openshell-gateway -n 100
```

An unreachable middleware can prevent registration/startup. Fix reachability
or protocol/configuration errors rather than bypassing TLS or fail-closed behavior.

## 7. Create a dedicated sandbox and check forwarding

```sh
openshell sandbox create \
  --name nomos-review-lab --from nomos-review-workload:lab \
  --policy examples/policy.yaml --no-auto-providers --no-tty --detach -- sleep infinity

openshell sandbox exec --name nomos-review-lab --no-tty -- \
  /usr/bin/curl --silent --show-error --include --max-time 10 \
  http://host.openshell.internal:18081/publish \
  --header 'content-type: application/json' \
  --data-binary '{"publication":"Our community garden welcomes visitors on Saturday."}'

curl --fail "http://$BRIDGE_IP:18081/requests"
```

The expected benign result is an allow with one new destination record. The
[policy](../examples/policy.yaml) restricts executable, host, port and route,
allows HTTP methods on `/publish`, and attaches two disclosure questions.
It sets network `enforcement: enforce` and middleware `on_error: fail_closed`:
these are separate controls. An allow from Nomos grants no new network access.
Only supported UTF-8 JSON object bodies can receive a model assessment.
OpenShell versions may reject extension HTTP methods before middleware runs.

With a real model, now send a fictional confidential disclosure. With the
simulated model, use [the deterministic checks](lab-testing.md#run-the-default-checks)
instead: its answers depend on the selected mode, not this text.

```sh
openshell sandbox exec --name nomos-review-lab --no-tty -- \
  /usr/bin/curl --silent --show-error --include --max-time 10 \
  http://host.openshell.internal:18081/publish \
  --header 'content-type: application/json' \
  --data-binary '{"publication":"Confidential FictionalCo plan: acquire ExampleCo next week for 42 million credits."}'

curl --fail "http://$BRIDGE_IP:18081/requests"
```

The intended result is a content denial and no new destination arrival. Actual
model scores determine the result: these synthetic examples are smoke checks,
not an accuracy guarantee. The example denies when either yes score is at least
0.7. See [threshold configuration](configuration.md#questions-in-sandbox-policy).

Continue with [lab testing](lab-testing.md) for the helper-driven checks,
additional routes, input rejections and real-model samples. You can also stop
the middleware temporarily to check outage
behavior. Keep destination counts and logs visible. A fail-closed outage should
produce no new arrival; it is an assessment failure rather than a content denial.
Restart the same middleware command afterward. Do not stop a shared service
outside this dedicated lab.

## Troubleshooting and cleanup

| Symptom | Check |
| --- | --- |
| Gateway cannot register Nomos | Running middleware, reachable 50051 address, protocol negotiation and valid registration |
| Supervisor cannot reach gateway | Gateway bridge bind, host alias and HTTPS callback; container loopback is not the host |
| Model call fails | Real endpoint/model IDs, credential permissions, trusted HTTPS roots and model API shape |
| Valid body is rejected | JSON object/encoding, complete protobuf cap, model context limit and timeout budgets |
| Denied route arrives | Effective `enforcement: enforce` and the middleware selector/attachment |
| Destination returns 507 | Capture store full; inspect/export evidence, then restart the dedicated destination |

Stop terminals A and B with Ctrl+C when finished. Remove the dedicated sandbox
using your OpenShell version's sandbox-delete command after inspecting its
records/logs. Keep model credentials, environments, logs and captured bodies out
of source control. The [automated tests](testing.md) validate local service
boundaries; this walkthrough must still be checked against your actual gateway
package, runtime images and chosen model endpoint.
