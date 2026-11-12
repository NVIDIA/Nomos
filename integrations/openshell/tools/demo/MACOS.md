<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Run the OpenShell and Nomos middleware demo on macOS

This package-based guide supports **Apple Silicon Macs (M-series), using a native
arm64 terminal**. OpenShell v0.1.6 does not provide the Intel macOS package used
by this installation path.

Use this page for the Mac setup, then run the same four-step presenter described
in [README.md](README.md). Do not use the Linux guide's bridge-IP bindings or
systemd commands on a Mac.

**Validation status:** the configuration below was checked against OpenShell
v0.1.6 source and package documentation. The presenter was tested against the
live Linux demo with configurable addresses. This exact Mac setup has not been
executed end to end here. An [independent public review](https://github.com/NVIDIA/Nomos/pull/137#pullrequestreview-5739934646)
validated the standalone integration on macOS with Docker Desktop and a newer
dev build, but did not record its exact networking configuration. Treat the
reachability and four-step checks below as required validation on your Mac.

## Why the addresses differ

Docker Desktop's bridge IP belongs to its Linux VM. A host process on your Mac
cannot bind to that address. In this setup, the host listeners bind to loopback,
while supervisor containers reach the Mac through Docker Desktop's host address.

| Connection | Address |
| --- | --- |
| Mac presenter to destination | `http://127.0.0.1:18081` |
| Mac gateway to middleware | `http://host.openshell.internal:50051`, resolving to `127.0.0.1` on the Mac |
| Supervisor to HTTPS gateway | `https://host.docker.internal:17670` |
| Supervisor to middleware/destination | `host.openshell.internal`, mapped by OpenShell to Docker Desktop's host address |

The `host_gateway_ip` setting supplies the two host aliases inside supervisor
containers. Step 2 resolves the Desktop host address inside Docker instead of
assuming that Docker's Linux bridge gateway is the Mac. The workload itself
remains `network=none`; OpenShell mediates its traffic.

## 1. Install and build

You need Homebrew, Python 3.11+, Git, curl, uv, Rust/rustup and the macOS compiler
tools. If the compiler tools are absent, install them with
`xcode-select --install`. No local GPU is required.

Start the Apple Silicon edition of Docker Desktop and select its local
`desktop-linux` Docker context. OpenShell v0.1.6 supports Docker Desktop or
Docker Engine 28.0+. This configuration uses bridge mode, so it does not
require Desktop host networking to be enabled.

Check that the terminal itself is native, rather than running under Rosetta:

```sh
uname -m
```

It must print `arm64`. If an M-series Mac prints `x86_64`, open a native terminal
before continuing. An Intel Mac is outside this package-based guide.

Install the pinned OpenShell package:

```sh
if [ "$(uname -m)" = "arm64" ]; then
  curl -LsSf https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh | \
    OPENSHELL_VERSION=v0.1.6 sh
  openshell --version
  openshell status
  brew services list
else
  printf 'Stop: use an Apple Silicon Mac with a native arm64 terminal.\n' >&2
fi
```

The installer uses Homebrew and initializes the HTTPS/authentication and
signing material. Retain that setup; do not copy another machine's keys or
disable authentication/TLS to bypass a startup problem. Keep the package's
matching supervisor and sandbox runtime images.

Clone the repository and build, or use an existing checkout containing this
guide. Run subsequent repository commands from `Nomos/integrations/openshell`
in every terminal:

```sh
git clone --branch develop https://github.com/NVIDIA/Nomos.git
cd Nomos/integrations/openshell
rustup toolchain install 1.95.0 --profile minimal --component rustfmt --component clippy
cargo build --locked --bin nomos-request-review
docker build -t nomos-review-workload:lab examples/curl-sandbox
```

The pinned Ubuntu 24.04 base image contains a `linux/arm64` manifest. Build
for the Mac's native Docker platform; do not force an amd64 workload.

## 2. Prepare settings and resolve the Desktop host address

```sh
export DEMO_DIR="$HOME/nomos-demo"
mkdir -p "$DEMO_DIR"
cp tools/demo/demo-config.macos.example.yaml "$DEMO_DIR/deployment.yaml"
DOCKER_DESKTOP_HOST_IP="$(docker run --rm --network bridge \
  nomos-review-workload:lab /usr/bin/getent ahostsv4 host.docker.internal | \
  awk 'NR==1 {print $1}')"
python3 -c 'import ipaddress,sys; print(ipaddress.IPv4Address(sys.argv[1]))' \
  "$DOCKER_DESKTOP_HOST_IP"
sed "s/DOCKER_DESKTOP_HOST_IP/$DOCKER_DESKTOP_HOST_IP/g" \
  tools/demo/gateway-macos.example.toml > "$DEMO_DIR/gateway-macos.resolved.toml"
```

The IP check must succeed. Do not substitute loopback or the Linux bridge IP.
Resolve this address again after a Desktop network reset/subnet change and
update the gateway configuration before creating new sandboxes.

The Mac gateway also needs to resolve the middleware's shared alias. Inspect it:

```sh
dscacheutil -q host -a name host.openshell.internal
```

If absent, add a Mac-local entry:

```sh
printf '127.0.0.1 host.openshell.internal\n' | sudo tee -a /etc/hosts
```

Correct an existing conflicting lab entry instead of adding duplicates. Verify:

```sh
python3 -c 'import socket; print(socket.gethostbyname("host.openshell.internal"))'
```

It should print `127.0.0.1` on the Mac. Supervisor containers will receive a
different mapping from `host_gateway_ip`; these two address contexts are intentional.

## 3. Start the destination: terminal A

From `Nomos/integrations/openshell`:

```sh
uv run --locked --no-dev --project tools/test-destination \
  test-destination --host 127.0.0.1 --port 18081
```

Leave it running. Captures are held in memory and contain only synthetic demo
requests. Restarting the destination clears them.

## 4. Start Nomos with Jev: terminal B

From `Nomos/integrations/openshell`, export your `JEV_API_KEY`, then run:

```sh
export DEMO_DIR="$HOME/nomos-demo"
: "${JEV_API_KEY:?Export your Jev API key before starting Nomos}"
export NOMOS_MODEL_TOKEN="$JEV_API_KEY"
export NOMOS_MODEL_ENDPOINT='https://api.typesafe.ai/v1/systemone'
export NOMOS_MODEL_ID='jev-1.14.0'
export NOMOS_EXPECTED_MODEL_ID='jev-1.14.0'
./target/debug/nomos-request-review \
  --bind 127.0.0.1:50051 \
  --model-timeout-ms 4000 --evaluation-timeout-ms 4500 \
  2>&1 | tee "$DEMO_DIR/middleware.log"
```

The `${JEV_API_KEY:?...}` check stops an empty-key startup before the token is
exported to Nomos. Leave it running. Because the listener binds to loopback, `--allow-insecure-lab`
is not needed. The gateway registration still needs `allow_insecure_transport = true`
for this plaintext local gRPC connection. Jev uses verified HTTPS; its API token
stays in the host middleware environment, not in the sandbox or settings file.
Use this service/log only for this dedicated demo while presenting.

## 5. Check reachability: terminal C

```sh
curl --fail --silent http://127.0.0.1:18081/health
docker run --rm --network bridge nomos-review-workload:lab \
  /usr/bin/curl --fail --silent --show-error --max-time 5 \
  http://host.docker.internal:18081/health
```

Both must return `{"status":"ok"}`. The second command is an ordinary Docker
health probe, not an OpenShell enforcement test; it submits no publication.

If the container cannot reach the loopback listener, stop here. Check Desktop's
host forwarding, firewall/VPN controls and local-network permissions. Record
the error before revising the setup; do not switch to unauthenticated all-interface
listeners or bypass OpenShell policy/TLS verification to make the probe pass.

## 6. Merge the gateway settings and restart its Homebrew service

The gateway reads `~/.config/openshell/gateway.toml` when present, otherwise
`$(brew --prefix)/var/openshell/gateway.toml`. An explicit
`OPENSHELL_GATEWAY_CONFIG` override can select another file; check your service
configuration if you have one.

Start with the installed package config so TLS, authentication and launch-signing
settings remain available. For the default paths:

```sh
export DEMO_DIR="$HOME/nomos-demo"
mkdir -p "$HOME/.config/openshell"
if [ ! -f "$HOME/.config/openshell/gateway.toml" ]; then
  cp "$(brew --prefix)/var/openshell/gateway.toml" \
    "$HOME/.config/openshell/gateway.toml"
fi
cp "$HOME/.config/openshell/gateway.toml" \
  "$DEMO_DIR/gateway.toml.before-macos-demo"
open -e "$HOME/.config/openshell/gateway.toml"
```

The resolved TOML file is a **merge fragment**, not a standalone gateway
configuration to preflight or launch. Merge its fields into the open package
configuration.
Do not replace the package configuration wholesale, duplicate existing table
headers or register a second service with the same name. The intended changes are:

- Gateway: Docker driver and loopback bind address.
- If multiple local container runtimes are installed, confirm the gateway uses
  the same Docker Desktop daemon as your CLI. Changing the CLI context does not
  configure a Homebrew service. If auto-detection selects the wrong daemon, set
  the supported `socket_path` field under `[openshell.drivers.docker]` to that
  Desktop context's Unix socket, using the actual local path rather than a
  hardcoded example.
- Docker driver: bridge supervisor, callback `https://host.docker.internal:17670`
  and the Desktop host IP resolved in step 2.
- Middleware registration: the supplied endpoint, byte cap and timeout, plus
  `binding_policy = "exact"` and `bindings = ["http_request_pre_credentials"]`.

If you need the explicit Docker Desktop socket, inspect the context:

```sh
docker context inspect desktop-linux --format '{{.Endpoints.docker.Host}}'
```

For a `unix://` endpoint, `socket_path` takes the filesystem path after that
prefix. Confirm the endpoint belongs to the intended local Desktop daemon;
leave `socket_path` unset when auto-detection already selects it correctly.

The binding settings require the expected request-review hook. They do not
replace sandbox network policy or its `on_error: fail_closed` setting.
The gateway TLS certificate generated by OpenShell's installation setup includes
`localhost` and `host.docker.internal` as valid hostnames, so keep hostname
verification enabled. Do not override
the package's runtime images with digests copied from another deployment.

Restarting the gateway affects its other workloads; use a dedicated lab installation.
With terminals A and B still running:

```sh
openshell-gateway config preflight --path "$HOME/.config/openshell/gateway.toml"
brew services restart openshell
brew services list
openshell gateway add https://localhost:17670 --local --name review-lab
openshell gateway select review-lab
openshell status
```

If `review-lab` already exists, inspect/reuse its correct registration or update
it with the supported `openshell gateway` commands instead of duplicating it.
If startup fails, inspect the Homebrew service logs/config; do not bypass mTLS
or certificate verification.

## 7. Create the sandbox and present

From `Nomos/integrations/openshell`:

```sh
openshell --gateway review-lab sandbox create \
  --name nomos-review-lab --from nomos-review-workload:lab \
  --policy examples/policy.yaml --no-auto-providers --no-tty --detach -- sleep infinity
openshell --gateway review-lab sandbox list
```

Wait until `nomos-review-lab` is Ready, then run:

```sh
export DEMO_DIR="$HOME/nomos-demo"
uv run --locked --project tools/demo python tools/demo/demo.py --config "$DEMO_DIR/deployment.yaml" --pause
```

All four steps must pass: one allowed publication, two Nomos denials and one
OpenShell route denial. The destination remains `host.openshell.internal:18081`
from the sandbox's perspective; the presenter inspects the same server through
Mac loopback. The script compares count differences, so previous captures do
not affect it. Avoid other requests or service restarts during a step.

Record `openshell --version`, `docker version` and the resulting
`$DEMO_DIR/validation-*.json` before calling this Mac setup validated.
Logs/settings/results/backups stay in `~/nomos-demo`, outside the source repo.
See the [presenter guide](README.md#5-present-and-inspect-the-evidence) for
single-step commands, output interpretation and offline tests.

## Sources

- [OpenShell v0.1.6 macOS installation](https://github.com/NVIDIA/OpenShell/blob/v0.1.6/docs/about/installation.mdx#macos)
- [Gateway configuration](https://github.com/NVIDIA/OpenShell/blob/v0.1.6/docs/how-it-works/gateways/configuration.mdx)
- [Docker host-address configuration](https://github.com/NVIDIA/OpenShell/blob/v0.1.6/crates/openshell-driver-docker/src/lib.rs)
- [Package certificate names](https://github.com/NVIDIA/OpenShell/blob/v0.1.6/crates/openshell-bootstrap/src/pki.rs)
- [Docker Desktop networking](https://docs.docker.com/desktop/features/networking/)
