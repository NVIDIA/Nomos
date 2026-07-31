#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Build the Nomos application images (council/monitor/executor/openshell/agent-signup)
# from this working tree and pre-pull + load the pinned public infra images, loading
# all of them into the Kind cluster for a service-mode bring-up (chart uses
# imagePullPolicy: IfNotPresent). Invoked by kind-up.sh.
#
# Usage: build-kind-images.sh [kind-cluster-name]   Env: IMAGE_TAG (default kind)
set -Eeuo pipefail

KIND_CLUSTER_NAME="${1:-${KIND_CLUSTER_NAME:-nomos}}"
IMAGE_TAG="${IMAGE_TAG:-kind}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

# Public infra images for service mode (pinned to the chart defaults). Pre-pulled
# and loaded so a service-mode bring-up does not depend on in-cluster registry pulls.
INFRA_IMAGES=(
  "redis:7-alpine"
  "openpolicyagent/opa:1.17.0"
  "nginxinc/nginx-unprivileged:1.27-alpine"
  "hashicorp/vault:1.17"
  "postgres:16-alpine"
)

build_image() {
  local dockerfile="$1"
  local image="$2"
  docker build -f "$ROOT_DIR/$dockerfile" -t "$image:$IMAGE_TAG" "$ROOT_DIR"
  kind load docker-image "$image:$IMAGE_TAG" --name "$KIND_CLUSTER_NAME"
}

load_infra_image() {
  local image="$1"
  docker image inspect "$image" >/dev/null 2>&1 || docker pull "$image"
  kind load docker-image "$image" --name "$KIND_CLUSTER_NAME"
}

# Application images. NOTE: the agent-signup service-mode console lives on the
# feat/onboarding-console branch — build this from a tree that includes it, or the
# agent-signup pod will CrashLoop under COUNCIL_STORAGE_MODE=postgres.
build_image deploy/Dockerfile nomos/council
build_image deploy/Dockerfile.monitor nomos/monitor
build_image deploy/Dockerfile.executor nomos/executor
build_image deploy/Dockerfile.openshell nomos/openshell
build_image deploy/Dockerfile.agent-signup nomos/agent-signup

for img in "${INFRA_IMAGES[@]}"; do
  load_infra_image "$img"
done

printf 'Loaded Nomos + infra images with tag %s into Kind cluster %s\n' "$IMAGE_TAG" "$KIND_CLUSTER_NAME"
