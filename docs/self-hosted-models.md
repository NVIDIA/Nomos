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

The example uses NVIDIA's `llama-3.1-8b-instruct` NIM image. Check the
[NIM prerequisites](https://docs.nvidia.com/nim/large-language-models/latest/get-started/prerequisites.html)
for a Linux host with an NVIDIA GPU, driver, and NVIDIA Container Toolkit.
That model needs at least 24 GB of GPU memory. You can select another image
and GPU count in `deploy/.env`; check its
[NIM support matrix](https://docs.nvidia.com/nim/large-language-models/latest/support-matrix.html)
for hardware requirements and its exact image tag. Evaluate the model's
authorization decisions with your own allowed and denied cases before using
it for privileged actions.

## Quick start: Docker Compose

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
profile. Re-running the launcher updates a profile it previously generated and
recreates the council so new role assignments take effect. Use `--skip-build`
after the first Nomos image build when the images have not changed.

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

The generated profile contains a `nim-local` backend and, in mixed mode, a
`nim-hosted` backend. Each role references one of them. All roles receive
request context needed for their decisions, so review the data sent to hosted
inference before choosing mixed mode. To use several local models or more
hosted backends, add backend definitions and change role references in a copy
of the profile, then deploy that file using the advanced controls below.

| Setting in `deploy/.env` | Purpose |
|---|---|
| `NVIDIA_API_KEY` | Real API Catalog key for mixed mode. Pure mode writes `local-nim` if absent to satisfy the existing bootstrapper; the local backend itself uses a literal placeholder. |
| `NGC_API_KEY` | Optional image or model-download credential for NIM; separate from inference authentication. |
| `NOMOS_NIM_IMAGE` | Exact model-specific NIM image and tag; default `nvcr.io/nim/meta/llama-3.1-8b-instruct:2.0.13`. |
| `NOMOS_NIM_GPU_COUNT` | GPUs reserved for NIM; default `1`. |
| `NOMOS_NIM_PORT` | Host port for NIM; default `8081`. |
| `NOMOS_LOCAL_MODEL_ID` | Select an ID if `/v1/models` lists more than one; it must match exactly. |
| `NOMOS_NIM_READY_TIMEOUT` | Seconds to wait for NIM startup and the first model download; default `900`. |

The `deploy/.env` file is gitignored and contains other Nomos secrets. Keep it
private. [NVIDIA's NIM installation guide](https://docs.nvidia.com/nim/large-language-models/latest/get-started/installation.html)
explains when NGC login or a model-source credential is required. For an image
that needs registry login, run `docker login nvcr.io` before the launcher.

## Use an existing self-hosted endpoint

You can operate the model server separately, including on another host or in
Kubernetes. Point a Nomos backend's `base_url` at its reachable
OpenAI-compatible `/v1` endpoint and set `model` to an ID returned by
`GET /v1/models`. The endpoint must support chat completions and the response
shape Nomos expects. `localhost` inside a council container or pod refers to
that container or pod, so use a service DNS name or reachable network address.
Use HTTPS and an appropriate credential when the endpoint crosses a trust
boundary.

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

```bash
# For a Compose profile, replace container DNS with the local published port
# in a temporary copy used by this host-side check.
sed 's#http://nim-llm:8000/v1#http://127.0.0.1:8081/v1#' \
  deploy/dynamic-config/council.yaml > /tmp/nomos-council-host-check.yaml
uv run --env-file deploy/.env python scripts/test_public_nim_smoke.py \
  --config /tmp/nomos-council-host-check.yaml
```

If you changed `NOMOS_NIM_PORT`, use that port in the temporary copy. The Kind
profile uses the bridge gateway and can be checked from a Kind pod. See
[Council Deployment and MCP Validation](council-mcp-validation.md) for deployed
behavior checks.

The NIM is an independent Compose project. The normal Nomos down scripts do
not stop it. Stop it separately when finished:

```bash
. deploy/scripts/dev-lib.sh
nim_project="${NOMOS_NIM_PROJECT:-$(env_get NOMOS_NIM_PROJECT)}"
docker compose -p "${nim_project:-nomos-nim}" --env-file deploy/.env \
  -f deploy/docker-compose.nim.yml down
```

The approach follows AI-Q's [self-hosted NIM model configuration](https://github.com/NVIDIA-AI-Blueprints/aiq/blob/develop/docs/source/customization/swapping-models.md#using-downloadable-nims-self-hosted)
and the Vulnerability Analysis blueprint's [Compose NIM overlay](https://github.com/NVIDIA-AI-Blueprints/vulnerability-analysis/blob/develop/deploy/docker-compose.nim.yml),
adapted to Nomos's per-role council profiles and current Compose and Kind
deployment scripts.
