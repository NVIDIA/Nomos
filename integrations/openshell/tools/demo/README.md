<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# OpenShell and Nomos middleware demo

Show how Nomos reviews an outgoing HTTP request that OpenShell network policy
already permits. The presenter sends curl requests from a dedicated sandbox,
shows the model scores and client response, and checks which requests actually
arrived at a local destination.

| Step | Expected result |
| --- | --- |
| Public workshop announcement | Nomos allows; destination returns HTTP 200; original bytes arrive intact |
| Populated account password | Nomos denies; destination receives nothing |
| Confidential acquisition plan | Nomos denies; destination receives nothing |
| Benign message sent to unapproved `/admin` | OpenShell policy denies before Nomos/Jev is called |

The two policy questions use a denial threshold of 0.70. All content is
fabricated, including the nonfunctional password. Running all four steps makes
three real Jev calls. Results can vary; this is an integration demonstration,
not an accuracy benchmark.

**Choose your platform:** the steps below target Ubuntu/Debian with native
Docker Engine. For an Apple Silicon Mac with Docker Desktop, follow [MACOS.md](MACOS.md)
instead; its service commands and host addresses differ.

## Get the code and prerequisites

```sh
git clone --branch develop https://github.com/NVIDIA/Nomos.git
cd Nomos/integrations/openshell
```

If you already have a checkout, use a revision containing this `tools/demo`
directory. No separate download or plugin repository is needed. Run every
repository command below from `Nomos/integrations/openshell`, including in
additional terminals.

You need Docker usable by your account, Python 3.11+, Git, curl, build-essential,
Rust/rustup, uv, and a host-side `JEV_API_KEY` for Jev. No GPU is required.
For a fixed OpenShell version, install **v0.1.6**:

```sh
curl -LsSf https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh | \
  OPENSHELL_VERSION=v0.1.6 sh
```

The v0.1.6 middleware protobuf files match the contracts vendored here, and its
Docker driver supports bridge mode and explicit binding policies. This is a
source compatibility check; the complete package-managed setup on v0.1.6 has
not been qualified by this demo. Keep its CLI, gateway and default runtime
images matched. See the [lab setup guide](../../docs/lab-setup.md) for package,
HTTPS and bridge-network details.

Use a dedicated local lab gateway: its configuration/restart affects every
sandbox it manages. Keep the package's HTTPS, authentication and signing setup;
do not copy another deployment's keys or database.

## 1. Build and prepare the local settings

```sh
rustup toolchain install 1.95.0 --profile minimal --component rustfmt --component clippy
cargo build --locked --bin nomos-request-review
docker build -t nomos-review-workload:lab examples/curl-sandbox

export DEMO_DIR="$HOME/nomos-demo"
mkdir -p "$DEMO_DIR"
BRIDGE_IP="$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')"
sed "s/BRIDGE_ADDRESS/$BRIDGE_IP/g" tools/demo/demo-config.example.yaml \
  > "$DEMO_DIR/deployment.yaml"
```

The image contains curl on Ubuntu 24.04 and runs as the unprivileged `ubuntu`
user. `deployment.yaml` tells the presenter which gateway, sandbox, destination
and middleware log to use. Its relative `middleware_log` path is resolved next
to the settings file, so output stays in `~/nomos-demo`, outside the checkout.
It contains no API credential.
The YAML examples include license headers as comments. The presenter commands
use `uv run --locked --project tools/demo` to install the locked YAML parser
dependency in an isolated environment.

Check the host aliases as described in [lab setup, step 3](../../docs/lab-setup.md#3-check-bridge-reachability).
The host must resolve both `host.openshell.internal` and `host.docker.internal`
to the bridge address; the Docker driver supplies both aliases inside
supervisors. Correct an existing entry rather than appending a conflicting
duplicate.

## 2. Start the destination: terminal A

```sh
BRIDGE_IP="$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')"
uv run --locked --no-dev --project tools/test-destination \
  test-destination --host "$BRIDGE_IP" --port 18081
```

Leave it running. The destination records allowed requests in memory; a restart
clears its history. This listener and the middleware's inbound gRPC listener
are unauthenticated development services. Restrict them to the host and trusted
bridge clients, and send only synthetic content.

## 3. Start Nomos with Jev: terminal B

Export your `JEV_API_KEY` in this terminal, then run:

```sh
export DEMO_DIR="$HOME/nomos-demo"
BRIDGE_IP="$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')"
: "${JEV_API_KEY:?Export your Jev API key before starting Nomos}"
export NOMOS_MODEL_TOKEN="$JEV_API_KEY"
export NOMOS_MODEL_ENDPOINT='https://api.typesafe.ai/v1/systemone'
export NOMOS_MODEL_ID='jev-1.14.0'
export NOMOS_EXPECTED_MODEL_ID='jev-1.14.0'
./target/debug/nomos-request-review \
  --bind "$BRIDGE_IP:50051" --allow-insecure-lab \
  --model-timeout-ms 4000 --evaluation-timeout-ms 4500 \
  2>&1 | tee "$DEMO_DIR/middleware.log"
```

The `${JEV_API_KEY:?...}` check stops an empty-key startup before the token is
exported to Nomos. Leave it running. Jev uses verified HTTPS. `--allow-insecure-lab` permits the
separate inbound gRPC listener on the isolated bridge; it does not disable
HTTPS verification to Jev. The token stays in the host middleware environment,
not the sandbox or settings file. Do not print it or supply it as a CLI argument.

Use this service/log only for this demo while presenting. Other requests or a
service restart during a step can invalidate its before/after evidence checks.

## 4. Configure the gateway and create the sandbox: terminal C

Follow [lab setup, step 6](../../docs/lab-setup.md#6-configure-the-package-managed-gateway--terminal-c)
to merge `examples/lab-gateway.toml` into the package-managed gateway config,
preflight the effective file, restart the gateway and register/select
`review-lab`. Preserve existing package authentication/TLS/signing settings;
do not replace a shared configuration wholesale.

The example explicitly requires the `http_request_pre_credentials` binding
with `binding_policy = "exact"`. OpenShell therefore checks that the service
advertises the request-review hook expected by this demo. This does not select
destinations or replace `on_error: fail_closed` in sandbox policy.

With terminals A and B still running, confirm the destination is reachable:

```sh
BRIDGE_IP="$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')"
curl --fail --silent "http://$BRIDGE_IP:18081/health"
openshell --gateway review-lab status
```

Then create the sandbox:

```sh
openshell --gateway review-lab sandbox create \
  --name nomos-review-lab --from nomos-review-workload:lab \
  --policy examples/policy.yaml --no-auto-providers --no-tty --detach -- sleep infinity
openshell --gateway review-lab sandbox list
```

Wait for `nomos-review-lab` to be Ready. If these names already exist, inspect
and reuse the intended demo resources or choose fresh names; do not delete an
unrelated sandbox. If you choose different names or ports, update
`$DEMO_DIR/deployment.yaml` to match the policy, services and gateway.

## 5. Present and inspect the evidence

```sh
export DEMO_DIR="$HOME/nomos-demo"
uv run --locked --project tools/demo python tools/demo/demo.py --config "$DEMO_DIR/deployment.yaml" --pause
```

Press Enter for each step. Omit `--pause` for a continuous check, or select one
step with `--case public`, `--case credential`, `--case confidential` or
`--case network-policy`.

The presenter verifies the HTTP response, middleware decision, model-call count
and destination arrival count. An allow must preserve the original method,
path and body bytes. Each successful run saves `validation-*.json` beside the
settings file. Those generated files and logs stay outside the source checkout.

The presenter allows up to one second for the middleware log writer to deliver
a terminal review event. This waits for local evidence only; it never resends
the HTTP request or retries a model call, and is excluded from client timing.
A missing event still fails the check.

The HTTP 200 response `{received, request_id, body_bytes}` comes from the local
test destination to curl inside the sandbox; `request_id` is the destination's
arrival number. It is not Jev's response or OpenShell's correlation ID.
The displayed Jev scores, token counts and evaluation timing come from the
middleware log. That log omits raw model requests and API tokens.

For a live log view in another host terminal:

```sh
tail -f "$HOME/nomos-demo/middleware.log"
```

To rehearse: explain the allowed public message first, then show the two content
denials, and finish with `/admin` to demonstrate that OpenShell network policy
still applies independently of a model assessment.

## Check the presenter without a gateway or API key

From `Nomos/integrations/openshell`:

```sh
uv run --locked --project tools/demo python -m unittest discover -s tools/demo -v
```

The offline tests use controlled responses. They test the presenter, not Jev's
model quality or the live sandbox integration.
