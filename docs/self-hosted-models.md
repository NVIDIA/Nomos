<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Deploy Nomos with Self-Hosted Models

Nomos selects a backend separately for its three voting members, code review,
and chairman. You can route all five roles to a local NVIDIA NIM, or mix that
NIM with NVIDIA API Catalog. The bundled launcher starts one NIM container and
deploys the existing Nomos service stack on Docker Compose or Kind. NIM runs
on the Docker host; Kind does not need GPU support inside its nodes.

## Before you start

On the Linux GPU host, install Docker Engine with the Compose v2 plugin, Bash
4.3 or newer, Python 3 with PyYAML, and curl. Start Docker and make sure your user can run
`docker` without `sudo`. For the Kind path, also install the `kind`, `kubectl`,
and `helm` CLIs. **Install Kind, but do not create a cluster manually:** the
launcher creates the Nomos cluster if it does not exist. See the
[general deployment prerequisites](getting-started.md#prerequisites) and the
[Kind installation guide](https://kind.sigs.k8s.io/docs/user/quick-start/)
for setup details. The Kind image loader requires Docker Engine API 1.48 or
newer for `docker save --platform` when recovering from a failed image import.

The example uses the pinned `nvcr.io/nim/openai/gpt-oss-20b:2.0.13` image and
its single-GPU MXFP4 profile (`vllm-mxfp4-tp1-pp1`). NVIDIA lists the A10G
among its verified GPUs; provide at least 24 GB of GPU memory and check the
[2.0.13 support matrix](https://docs.nvidia.com/nim/large-language-models/2.0.13/reference/support-matrix.html#gpt-oss-20b)
for your card and profile. Check the
[NIM prerequisites](https://docs.nvidia.com/nim/large-language-models/latest/get-started/prerequisites.html)
for a Linux host with an NVIDIA GPU, driver, and NVIDIA Container Toolkit.
NIM selects a compatible profile automatically. You can select another image
and GPU count in `deploy/.env`; check its support matrix for hardware
requirements and its exact image tag. Allow disk space and network
access for the first model download and container image builds and pulls. The
Kind cluster maps host ports 80 and 443; NIM binds to port 8081 by default.
Pure mode needs no hosted inference API key. Mixed mode requires a real
`NVIDIA_API_KEY`; some NIM images or model downloads also require NGC access.

Evaluate the model's authorization decisions with your own allowed and denied
cases before using it for privileged actions. The bundled hosted and self-hosted
models are onboarding examples. Select production models based on your use case,
measured authorization quality, latency, hardware, and licensing requirements;
see the [model-selection discussion](reference/configuration.md#one-model-by-default-several-if-you-want-them).
The GPT-OSS weights use [Apache 2.0](https://openai.com/open-models/); the
[NIM container has separate NVIDIA terms](https://catalog.ngc.nvidia.com/orgs/nim/openai/containers/gpt-oss-20b/2.0.13/governing-terms).

## Quick start: Docker Compose

The Compose bootstrap shares `deploy/dynamic-config` with the council through
`COUNCIL_CONFIG_GID`, persisted in `deploy/.env`. It defaults to your primary
group. On shared hosts, set this to the numeric ID of a dedicated Nomos group
that your account belongs to; every member of that group can modify council
configuration. Owned directories use setgid so new profiles inherit this
group, and the bootstrap removes access for other users. Do not use a group
shared with untrusted accounts.

From the repository root, choose one routing mode:

```bash
# All council roles use the local NIM. No hosted inference key is needed.
deploy/scripts/self-hosted-up.sh --target compose --mode pure

# Three roles use local NIM; two use NVIDIA API Catalog.
# First put a real NVIDIA_API_KEY in deploy/.env (or export it in this shell).
deploy/scripts/self-hosted-up.sh --target compose --mode mixed
```

The launcher starts NIM in its own Compose project, waits for `/v1/models`,
uses the returned model ID, writes the live profile to the gitignored
`deploy/dynamic-config/council.yaml`, then starts and verifies the normal
service-mode Nomos stack. The council reaches NIM at `http://nim-llm:8000/v1`
over a private Docker network. The host-facing NIM port binds to
`127.0.0.1:8081` by default.

If a custom live council profile already exists, the launcher stops before
replacing it. Back up and remove that file if you want the generated routing
profile. If the existing `local_council_catalog.yaml` points `council-prod`
at a managed profile (for example, `councils/council-prod.yaml` after
`update-from-file`), restore its default council and `council-prod` reference
to `council.yaml` after backing it up. Remove any other fields on that catalog
entry, because they can override the generated profile. The launcher checks this before starting
NIM so it cannot report routing that the council does not load. Re-running the
launcher updates a profile it previously generated and
recreates the council so new role assignments take effect. Use `--skip-build`
after the first Nomos image build when the images have not changed.

If account registration reports that `dev-agent` exists without a usable
token, rerun with a new agent ID. The old token cannot be recovered from the
Compose database:

```bash
deploy/scripts/self-hosted-up.sh --target compose --mode pure --skip-build \
  --agent-id my-compose-agent
```

Choose an ID that has not already been registered in this Compose deployment.
The launcher saves its new token in `deploy/.env`. Keep a private copy of that
file first if its current token belongs to another deployment target.

## Quick start: Kind

On the same Linux GPU host, with Docker as the Kind provider:

```bash
# All council roles use the local NIM.
deploy/scripts/self-hosted-up.sh --target kind --mode pure

# Mixed local and hosted routing; requires a real NVIDIA_API_KEY.
deploy/scripts/self-hosted-up.sh --target kind --mode mixed
```

The launcher creates the Kind cluster if needed, finds the gateway address of
Docker's `kind` bridge, publishes NIM only on that host address, and checks
`/v1/models` from a temporary Kind pod. It then passes a generated profile to
the existing Helm chart as `council.configOverride`. The profile is stored at
`deploy/dynamic-config/self-hosted-council.yaml`; the shipped chart profile
stays intact. A change to the profile triggers a council rollout on Helm
upgrade. Kind's default network policy setting permits the pod's connection to
the host NIM. If you enable an enforcing CNI or host firewall, allow the
council pods to reach the NIM port on the Docker bridge gateway.

This path discovers the reachable Docker bridge address rather than assuming
`localhost` or `host.docker.internal` works inside a pod. If the pod probe
fails, check `docker network inspect kind`, the host firewall, and NIM logs.
The launcher will not deploy Nomos with an unreachable NIM endpoint.

## Connect after deployment

On success, the launcher prints `Nomos compose deployment uses pure routing`
or `Nomos kind deployment uses pure routing` (with `mixed` for a split), followed
by the generated profile path. Use the matching MCP endpoint:

| Target | MCP URL | HTTP authorization |
|---|---|---|
| Compose service mode | `http://localhost:9900/mcp` | `Authorization: Bearer <MONITOR_MCP_PROXY_TOKEN>` |
| Kind | `http://mcp.nomos.local/mcp` | `Authorization: Bearer <MONITOR_MCP_PROXY_TOKEN>` |

Read `MONITOR_MCP_PROXY_TOKEN` from the private `deploy/.env`. The launcher also
saves the demo account's `NOMOS_AGENT_ID` and `NOMOS_AUTH_TOKEN` there; supply
that pair when calling council tools that require agent identity. For Kind,
add `127.0.0.1 mcp.nomos.local` to `/etc/hosts` as described in the
[Kind connection guide](../deploy/kubernetes/README.md). Continue with the
[end-to-end agent request](getting-started.md#6-make-an-end-to-end-request), using the
MCP URL above in place of that example's default `9901/mcp` URL. On Kind the
example also sends the Writ to `127.0.0.1:9907`, which the launcher does not
forward; open it first with
`kubectl --context kind-nomos -n nomos port-forward svc/nomos-executor-openshell 9907:9907`.

## Routing and credentials

The default mixed assignment is:

| Role | Pure mode | Mixed mode |
|---|---|---|
| `command-integrity` voter | Local NIM | Local NIM |
| `environment-isolation` voter | Local NIM | NVIDIA API Catalog |
| `blast-containment` voter | Local NIM | NVIDIA API Catalog |
| `code_review` | Local NIM | Local NIM |
| `chairman` | Local NIM | Local NIM |

In mixed mode, select another split with `--local-roles`, using a comma
separated list of these role names. At least one role must remain local and
one hosted. For example:

```bash
deploy/scripts/self-hosted-up.sh --target compose --mode mixed \
  --local-roles command-integrity,environment-isolation,code_review
```

The generated profile contains a `nim-local` backend. In mixed mode, roles
that remain hosted keep their backend names, models, endpoints, and credentials
from the source council configuration. Only backends used by hosted roles are
retained, so unused hosted credentials are not required in pure mode. The local
backend uses the NIM provider and inherits tuning settings from the source
command-integrity backend. All roles receive
request context needed for their decisions, so review the data sent to hosted
inference before choosing mixed mode.

The launcher manages one local model server. Every local role (all roles in pure
mode, or those selected by `--local-roles` in mixed mode) shares that server's
endpoint and model ID. To use several local models, provision their servers
separately and create a custom profile with each backend's model, endpoint,
credentials, and role assignments. The routing excerpt below omits role policy
fields: keep each role's existing priorities, prompts, and other policy settings
when updating a copy of the profile:

```yaml
llm_backends:
  local-command:
    provider: nim
    model: example/command-model
    base_url: https://command-model.example.com/v1
    api_key: "${COMMAND_MODEL_API_KEY}"
  local-review:
    provider: nim
    model: example/review-model
    base_url: https://review-model.example.com/v1
    api_key: "${REVIEW_MODEL_API_KEY}"
members:
  - id: command-integrity
    backend: local-command
  - id: environment-isolation
    backend: local-review
  - id: blast-containment
    backend: local-review
code_review:
  enabled: true
  backend: local-review
chairman:
  enabled: true
  backend: local-command
```

Replace the example model IDs and URLs with those of your servers, and make
both API key environment variables available to the council. For a server
that requires no authentication, use the literal placeholder `local-nim`.
Deploy the custom profile using the advanced controls below.

| Setting in `deploy/.env` | Purpose |
|---|---|
| `NVIDIA_API_KEY` | Real API Catalog key for mixed mode. Pure mode writes `local-nim` if absent to satisfy the existing bootstrapper; the local backend itself uses a literal placeholder. |
| `NGC_API_KEY` | Optional image or model-download credential for NIM; separate from inference authentication. |
| `NOMOS_NIM_IMAGE` | Exact model-specific NIM image and tag; default `nvcr.io/nim/openai/gpt-oss-20b:2.0.13`. |
| `NOMOS_NIM_GPU_COUNT` | GPUs reserved for NIM; default `1`. |
| `NOMOS_NIM_PORT` | Host port for NIM; default `8081`. |
| `NOMOS_LOCAL_MODEL_ID` | Select an ID if `/v1/models` lists more than one; it must match exactly. |
| `NOMOS_NIM_READY_TIMEOUT` | Seconds to wait for NIM startup and the first model download; default `900`. |

The `deploy/.env` file is gitignored and contains other Nomos secrets. Keep it
private. [NVIDIA's NIM installation guide](https://docs.nvidia.com/nim/large-language-models/latest/get-started/installation.html)
explains when NGC login or a model-source credential is required. For an image
that needs registry login, run `docker login nvcr.io` before the launcher.

### Switching between Compose and Kind

Both targets save the demo account's `NOMOS_AGENT_ID` and `NOMOS_AUTH_TOKEN` in
the same `deploy/.env`, but each has its own PostgreSQL account store. A token
created by one target does not authenticate an account in the other. Re-running
a target reuses its existing account only when the saved token verifies against
that target's store. If the token is missing or belongs to the other target,
the launcher stops without changing the existing account.

To test both targets from one checkout, use a distinct agent ID for each. For
example, after a Compose run, start Kind with:

```bash
deploy/scripts/self-hosted-up.sh --target kind --mode mixed \
  --agent-id kind-mixed-agent
```

The new account's token replaces the `NOMOS_AGENT_ID`/`NOMOS_AUTH_TOKEN` pair in
`deploy/.env`. Save a private, gitignored copy of the file before switching
targets if you need to restore the previous target's credential later. If an
existing account's token was lost, you can instead choose a new `--agent-id` or
deliberately run with `AGENT_FORCE_ROTATE=1`; rotation invalidates that
account's old token.

## Select a GPU on a multi-GPU host

The default `NOMOS_NIM_GPU_COUNT=1` reserves one available GPU but does not
select which one. To pin NIM to a particular card, list the host GPU UUIDs:

```bash
nvidia-smi --query-gpu=index,name,pci.bus_id,uuid --format=csv
```

In `deploy/docker-compose.nim.yml`, replace the `count` line in the `nim-llm`
device reservation with the UUID of the chosen GPU:

```yaml
device_ids: ["GPU-your-chosen-uuid"]
```

Use `device_ids` **instead of** `count`; Docker Compose rejects a reservation
containing both. A UUID identifies the same card if host GPU indices change.
This selection applies to both the Compose and Kind quick starts because NIM
runs as a Compose container on the GPU host in either case. After launch, check
which GPU the container sees:

```bash
docker compose --env-file deploy/.env \
  -f deploy/docker-compose.nim.yml exec nim-llm \
  nvidia-smi --query-gpu=uuid,name --format=csv
```

## Use an existing self-hosted endpoint

You can operate the model server separately, including on another host or in
Kubernetes. Point a Nomos backend's `base_url` at its reachable
OpenAI-compatible `/v1` endpoint and set `model` to an ID returned by
`GET /v1/models`. The endpoint must support chat completions and the response
shape Nomos expects. `localhost` inside a council container or pod refers to
that container or pod, so use a service DNS name or reachable network address.
Use HTTPS and an appropriate credential when the endpoint crosses a trust
boundary.

For a new Compose deployment, this example routes all five roles to one
OpenAI-compatible endpoint. Replace the model ID with a value from its
`GET /v1/models` response and the URL with an address reachable from the
council container. Put the endpoint's API token in `NVIDIA_API_KEY` in the
private `deploy/.env` file; use `local-nim` for an endpoint that requires no
authentication. The profile references that value without storing the token.

```bash
export NOMOS_ENDPOINT_MODEL='openai/gpt-oss-20b'
export NOMOS_ENDPOINT_URL='https://models.example.com/v1'
umask 077
python3 - <<'PY'
import os
from pathlib import Path

import yaml

profile = Path("deploy/dynamic-config/council.yaml")
if profile.exists():
    raise SystemExit(f"Back up and edit the existing profile instead: {profile}")
profile.parent.mkdir(parents=True, exist_ok=True)
config = yaml.safe_load(Path("configs/council.yaml").read_text())
backend = dict(config["llm_backends"]["nim-kimi"])
backend.update(
    provider="nim",
    model=os.environ["NOMOS_ENDPOINT_MODEL"],
    base_url=os.environ["NOMOS_ENDPOINT_URL"],
    api_key="${NVIDIA_API_KEY}",
)
config["llm_backends"] = {"existing-endpoint": backend}
for member in config["members"]:
    member["backend"] = "existing-endpoint"
config["code_review"]["backend"] = "existing-endpoint"
config["chairman"]["backend"] = "existing-endpoint"
profile.write_text(yaml.safe_dump(config, sort_keys=False))
PY
deploy/scripts/compose-up.sh -y
```

If `local_council_catalog.yaml` already exists, its default `council-prod`
entry must reference `council.yaml` without role overrides. Back up any managed
catalog or profile before changing it. The Compose gateway is then available
at `http://localhost:9900/mcp` with the credentials described above.

For Compose, edit the live `deploy/dynamic-config/council.yaml`; the bootstrapper
seeds it from `configs/council.yaml` only when absent. Recreate the council
after editing a bind-mounted profile. For Kind's default `storage.mode=service`,
pass an external file with `KIND_COUNCIL_PROFILE_FILE=/absolute/path.yaml` to
`deploy/kubernetes/scripts/kind-up.sh` or `scripts/deploy_stack.sh`; the chart
stores it in the council ConfigMap and rolls out a new pod when it changes.
For a Helm deployment with `storage.mode=pvc`, edit the live
`/app/dynamic-config/council.yaml` on its dynamic-config claim. See
[Changing the Council Model Configuration](deployment-setup.md#changing-the-council-model-configuration)
for profile and Secret details. Each member, chairman, and code-review backend
reference can point to a different model, whether locally or publicly hosted.
Remove unused active backends that reference missing environment variables.

## Verify and stop

Both launch commands run functional deployment verification. To inspect
routing, check the generated profile and compare model requests with NIM logs
and hosted provider usage. You can also run the
[endpoint compatibility check](../scripts/test_public_nim_smoke.py) from a
network that can reach every selected endpoint; it sends real inference
requests to each role:

For Docker Compose, the generated profile uses container DNS, so make a
host-reachable copy before running the check from the Linux host:

```bash
sed 's#http://nim-llm:8000/v1#http://127.0.0.1:8081/v1#' \
  deploy/dynamic-config/council.yaml > /tmp/nomos-council-host-check.yaml
uv run --extra openai --env-file deploy/.env python scripts/test_public_nim_smoke.py \
  --config /tmp/nomos-council-host-check.yaml
```

If you changed `NOMOS_NIM_PORT`, use that port in the temporary copy.

For Kind, use its generated profile directly from the same Linux GPU host. It
already contains the reachable Docker bridge gateway address:

```bash
uv run --extra openai --env-file deploy/.env python scripts/test_public_nim_smoke.py \
  --config deploy/dynamic-config/self-hosted-council.yaml
```

This endpoint check tests the selected model roles; the launcher's functional
verification tests the deployed stack. See
[Council Deployment and MCP Validation](council-mcp-validation.md) for deployed
behavior checks.

The NIM is an independent Compose project. The normal Nomos down scripts do
not stop it. For Compose, stop Nomos first so its council releases the shared
`nomos-nim` network. Keep Nomos volumes when you want to retain the account
store and other saved data. The dev Vault is in-memory and resets on shutdown:

```bash
deploy/scripts/compose-down.sh --keep-volumes
```

For a fresh Compose deployment when you no longer need its saved data, omit
`--keep-volumes`:

```bash
deploy/scripts/compose-down.sh
```

This deletes all Nomos Compose volumes, including Postgres data and its agent
accounts. The next launch can create `dev-agent` again and save a new token in
`deploy/.env`. It does not reset a Kind account or the separate NIM model cache.

For Kind, stop the cluster with `deploy/kubernetes/scripts/kind-down.sh`.
Then stop NIM for either target:

```bash
. deploy/scripts/dev-lib.sh
nim_project="${NOMOS_NIM_PROJECT:-$(env_get NOMOS_NIM_PROJECT)}"
docker compose -p "${nim_project:-nomos-nim}" --env-file deploy/.env \
  -f deploy/docker-compose.nim.yml down
```

If you stopped NIM before the Compose council, Docker may report `Network
nomos-nim Resource is still in use`. The NIM container has stopped; stop the
Nomos stack and rerun the NIM `down` command to remove the remaining network.
The NIM model cache volume remains available for the next launch.
