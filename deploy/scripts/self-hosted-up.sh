#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Start a local NIM, generate the council routing profile, and deploy Nomos.
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./dev-lib.sh
. "$SCRIPT_DIR/dev-lib.sh"

usage() {
  cat <<'EOF'
Usage: deploy/scripts/self-hosted-up.sh --target compose|kind --mode pure|mixed [options]

Options:
  --local-roles CSV   Mixed mode roles sent to local NIM (default:
                      command-integrity,code_review,chairman)
  --agent-id ID       Demo agent to create or reuse (default: dev-agent)
  --skip-build        Reuse existing Nomos images

Set NVIDIA_API_KEY in deploy/.env or export it for mixed mode.
Optional NIM settings in deploy/.env: NGC_API_KEY, NOMOS_NIM_IMAGE,
NOMOS_NIM_GPU_COUNT, NOMOS_NIM_PORT,
NOMOS_LOCAL_MODEL_ID. See docs/self-hosted-models.md for prerequisites.
EOF
}

target="" mode="" local_roles="" agent_id="" skip_build=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --target) target="${2:?missing target}"; shift ;;
    --target=*) target="${1#*=}" ;;
    --mode) mode="${2:?missing mode}"; shift ;;
    --mode=*) mode="${1#*=}" ;;
    --local-roles) local_roles="${2:?missing roles}"; shift ;;
    --local-roles=*) local_roles="${1#*=}"; [[ -n "$local_roles" ]] || die "missing roles" ;;
    --agent-id) agent_id="${2:?missing agent ID}"; shift ;;
    --agent-id=*) agent_id="${1#*=}"; [[ -n "$agent_id" ]] || die "missing agent ID" ;;
    --skip-build) skip_build=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done
[[ "$target" = compose || "$target" = kind ]] || die "--target must be compose or kind"
[[ "$mode" = pure || "$mode" = mixed ]] || die "--mode must be pure or mixed"
[[ "$mode" = mixed || -z "$local_roles" ]] || die "--local-roles requires --mode mixed"
require_cmd docker curl python3
docker info >/dev/null 2>&1 || die "docker daemon is not available"
docker compose version >/dev/null 2>&1 || die "docker compose (v2 plugin) is required"
if [[ "$target" = kind ]]; then
  require_cmd kind kubectl helm
  # The NIM port is bound to this Docker bridge's host gateway, so require the
  # Docker provider even on hosts that also have Podman installed.
  export KIND_EXPERIMENTAL_PROVIDER=docker
fi

# The regular bootstrappers still require a nonempty key in deploy/.env. A
# local-only profile uses a literal placeholder; mixed routing needs the real
# hosted credential and is checked before starting the model container.
key="$(env_get NVIDIA_API_KEY)"
if [[ -n "${NVIDIA_API_KEY:-}" && "$NVIDIA_API_KEY" != "$key" ]]; then
  env_set NVIDIA_API_KEY "$NVIDIA_API_KEY"
  key="$NVIDIA_API_KEY"
fi
if [[ "$mode" = mixed ]]; then
  [[ -n "$key" && "$key" != local-nim && "$key" != nvapi-your-key-here ]] \
    || die "mixed mode requires a real NVIDIA_API_KEY in deploy/.env"
elif [[ -z "$key" ]]; then
  env_set NVIDIA_API_KEY local-nim
fi

export NOMOS_NIM_PROJECT="${NOMOS_NIM_PROJECT:-$(env_get NOMOS_NIM_PROJECT)}"
export NOMOS_NIM_NETWORK="${NOMOS_NIM_NETWORK:-$(env_get NOMOS_NIM_NETWORK)}"
export NOMOS_NIM_PORT="${NOMOS_NIM_PORT:-$(env_get NOMOS_NIM_PORT)}"
NOMOS_NIM_PROJECT="${NOMOS_NIM_PROJECT:-nomos-nim}"
NOMOS_NIM_NETWORK="${NOMOS_NIM_NETWORK:-nomos-nim}"
NOMOS_NIM_PORT="${NOMOS_NIM_PORT:-8081}"
[[ "$NOMOS_NIM_PORT" =~ ^[0-9]+$ ]] && (( NOMOS_NIM_PORT >= 1 && NOMOS_NIM_PORT <= 65535 )) \
  || die "NOMOS_NIM_PORT must be a valid TCP port"
local_model="${NOMOS_LOCAL_MODEL_ID:-$(env_get NOMOS_LOCAL_MODEL_ID)}"
export NOMOS_LOCAL_MODEL_ID="$local_model"

if [[ "$target" = kind ]]; then
  if ! kind get clusters 2>/dev/null | grep -qx "$KIND_CLUSTER_NAME"; then
    log "Creating Kind cluster '$KIND_CLUSTER_NAME' to discover its Docker bridge"
    kind create cluster --name "$KIND_CLUSTER_NAME" \
      --config "$ROOT_DIR/deploy/kubernetes/kind-cluster.yaml"
  fi
  network_json="$(docker network inspect kind)" \
    || die "could not inspect the Kind Docker network; check 'docker network inspect kind'"
  gateway="$(printf '%s' "$network_json" | python3 -c '
import ipaddress, json, sys

for network in json.load(sys.stdin):
    for config in network.get("IPAM", {}).get("Config", []):
        address = config.get("Gateway", "")
        try:
            if isinstance(ipaddress.ip_address(address), ipaddress.IPv4Address):
                print(address)
                sys.exit(0)
        except ValueError:
            pass
sys.exit(1)
')" || die "Kind Docker network has no IPv4 gateway; inspect its IPAM config with 'docker network inspect kind --format {{json .IPAM.Config}}'"
  export NOMOS_NIM_BIND_ADDR="$gateway"
  council_url="http://$gateway:$NOMOS_NIM_PORT/v1"
  profile="$ROOT_DIR/deploy/dynamic-config/self-hosted-council.yaml"
else
  export NOMOS_NIM_BIND_ADDR=127.0.0.1
  council_url="http://nim-llm:8000/v1"
  profile="$ROOT_DIR/deploy/dynamic-config/council.yaml"
fi
if [[ -f "$profile" ]] && ! head -n1 "$profile" | grep -Fq 'Generated by deploy/scripts/render-self-hosted-council.py'; then
  die "existing custom council profile at $profile; back it up and remove it before using this launcher"
fi

log "Starting local NIM (${NOMOS_NIM_PROJECT}, port ${NOMOS_NIM_BIND_ADDR}:${NOMOS_NIM_PORT})"
nim_compose_args=(--project-name "$NOMOS_NIM_PROJECT" --env-file "$ENV_FILE"
  -f "$ROOT_DIR/deploy/docker-compose.nim.yml")
nim_running="$(docker compose "${nim_compose_args[@]}" ps -q --status running nim-llm)"
_nim_started_by_us=0
if [[ -z "$nim_running" ]]; then
  _nim_started_by_us=1
fi
cleanup_nim_on_failure() {
  local status=$?
  if (( status != 0 && _nim_started_by_us == 1 )); then
    docker compose "${nim_compose_args[@]}" stop nim-llm \
      || warn "could not stop NIM after launch failed; stop it with the NIM Compose project"
  fi
}
trap cleanup_nim_on_failure EXIT
docker compose "${nim_compose_args[@]}" up -d nim-llm

ready_url="http://${NOMOS_NIM_BIND_ADDR}:${NOMOS_NIM_PORT}/v1/models"
ready_timeout="${NOMOS_NIM_READY_TIMEOUT:-900}"
[[ "$ready_timeout" =~ ^[0-9]+$ ]] || die "NOMOS_NIM_READY_TIMEOUT must be seconds"
deadline=$((SECONDS + ready_timeout))
log "Waiting for the NIM model list (first download can take several minutes)"
models_json=""
while (( SECONDS <= deadline )); do
  if models_json="$(curl -fsS --max-time 5 "$ready_url" 2>/dev/null)"; then
    break
  fi
  sleep 5
done
[[ -n "$models_json" ]] || die "NIM did not become ready at $ready_url; inspect docker compose -p $NOMOS_NIM_PROJECT logs nim-llm"
model_id="$(printf '%s' "$models_json" | python3 -c '
import json, os, sys
data = json.load(sys.stdin)
ids = [entry["id"] for entry in data.get("data", []) if isinstance(entry, dict) and isinstance(entry.get("id"), str)]
requested = os.environ.get("NOMOS_LOCAL_MODEL_ID", "")
if requested:
    if requested not in ids:
        sys.exit(f"NOMOS_LOCAL_MODEL_ID {requested!r} is absent from /v1/models: {ids}")
    print(requested)
elif len(ids) == 1:
    print(ids[0])
else:
    sys.exit(f"NIM returned {len(ids)} model IDs; set NOMOS_LOCAL_MODEL_ID to one of: {ids}")
')" || die "could not select a model from NIM /v1/models"
ok "NIM serves model $model_id"

if [[ "$target" = kind ]]; then
  log "Checking the NIM endpoint from a Kind pod"
  probe_name="nomos-nim-probe-$$"
  if ! kc run "$probe_name" --image=curlimages/curl:8.14.1 \
      --restart=Never --rm -i --quiet --pod-running-timeout=90s \
      --command -- curl -fsS --max-time 15 "$council_url/models" >/dev/null; then
    kc delete pod "$probe_name" --ignore-not-found --wait=false >/dev/null 2>&1 || true
    die "Kind pods cannot reach $council_url; check the Docker bridge and host firewall"
  fi
  ok "Kind pods can reach the NIM"
fi

render_args=(--output "$profile" --local-model "$model_id" --local-url "$council_url" --mode "$mode")
[[ -z "$local_roles" ]] || render_args+=(--local-roles "$local_roles")
python3 "$SCRIPT_DIR/render-self-hosted-council.py" "${render_args[@]}"

if [[ "$target" = kind ]]; then
  export KIND_COUNCIL_PROFILE_FILE="$profile"
  kind_args=(-y)
  (( skip_build == 0 )) || kind_args+=(--skip-build)
  [[ -z "$agent_id" ]] || kind_args+=(--agent-id "$agent_id")
  "$ROOT_DIR/deploy/kubernetes/scripts/kind-up.sh" "${kind_args[@]}"
else
  service_overlay="$ROOT_DIR/deploy/docker-compose.service.yml"
  nim_overlay="$ROOT_DIR/deploy/docker-compose.nim-connect.yml"
  export COMPOSE_EXTRA_FILES="$service_overlay:$nim_overlay${COMPOSE_EXTRA_FILES:+:$COMPOSE_EXTRA_FILES}"
  export COUNCIL_CONFIG_FILE="$profile"
  compose_args=(-y)
  (( skip_build == 0 )) || compose_args+=(--skip-build)
  [[ -z "$agent_id" ]] || compose_args+=(--agent-id "$agent_id")
  # On a rerun, a bind-mounted profile change alone does not change the
  # Compose service spec. Reload it before compose-up performs verification.
  if [[ -n "$(dc ps -q council 2>/dev/null)" ]]; then
    dc up -d --no-deps --force-recreate council
  fi
  "$SCRIPT_DIR/compose-up.sh" "${compose_args[@]}"
fi

ok "Nomos $target deployment uses $mode routing (profile: $profile)"
trap - EXIT
