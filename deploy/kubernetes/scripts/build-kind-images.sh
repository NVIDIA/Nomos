#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


set -Eeuo pipefail

KIND_CLUSTER_NAME="${1:-${KIND_CLUSTER_NAME:-nomos}}"
IMAGE_TAG="${IMAGE_TAG:-kind}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

build_image() {
  local dockerfile="$1"
  local image="$2"
  docker build -f "$ROOT_DIR/$dockerfile" -t "$image:$IMAGE_TAG" "$ROOT_DIR"
  kind load docker-image "$image:$IMAGE_TAG" --name "$KIND_CLUSTER_NAME"
}

build_image deploy/Dockerfile nomos/council
build_image deploy/Dockerfile.monitor nomos/monitor
build_image deploy/Dockerfile.executor nomos/executor
build_image deploy/Dockerfile.openshell nomos/openshell
build_image deploy/Dockerfile.agent-signup nomos/agent-signup
build_image deploy/Dockerfile.demo nomos/demo

printf 'Loaded Nomos images with tag %s into Kind cluster %s\n' "$IMAGE_TAG" "$KIND_CLUSTER_NAME"
