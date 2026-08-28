#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# shellcheck shell=bash

# Shared Kind ingress availability checks. The caller supplies the name of its
# kubectl wrapper because deploy_stack.sh and the standalone Kind scripts use
# different context helpers.

kind_cluster_has_nginx_ingress_class() {
  local kubectl_cmd="$1"
  "$kubectl_cmd" get ingressclass nginx >/dev/null 2>&1
}

kind_cluster_has_ingress_ready_node() {
  local kubectl_cmd="$1"
  # deploy/kubernetes/kind-cluster.yaml labels the control-plane node and maps
  # host ports 80/443 at creation; neither can be added to an existing cluster.
  [[ -n "$("$kubectl_cmd" get nodes -l ingress-ready=true -o name 2>/dev/null)" ]]
}

kind_cluster_maps_host_ingress_ports() {
  # Exact host ports matter because *.nomos.local uses implicit ports 80/443.
  local port
  for port in 80 443; do
    docker port "${KIND_CLUSTER_NAME}-control-plane" "${port}/tcp" 2>/dev/null \
      | grep -q ":${port}\$" || return 1
  done
}

kind_cluster_has_usable_ingress() {
  local kubectl_cmd="$1"
  kind_cluster_has_nginx_ingress_class "$kubectl_cmd" \
    && kind_cluster_has_ingress_ready_node "$kubectl_cmd" \
    && kind_cluster_maps_host_ingress_ports
}
