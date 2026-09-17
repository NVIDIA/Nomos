# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# shellcheck shell=bash
#
# SOURCE this file; do not run it. (A shebang below the SPDX header would be an
# inert comment, so there is deliberately none.)

# Shared Kind ingress availability checks. The caller supplies the name of its
# kubectl wrapper because deploy_stack.sh and the standalone Kind scripts use
# different context helpers.

kind_cluster_has_nginx_ingress_class() {
  local kubectl_cmd="$1"
  local ingress_class
  if ! ingress_class="$("$kubectl_cmd" get ingressclass nginx --ignore-not-found -o name 2>/dev/null)"; then
    return 2
  fi
  [[ -n "$ingress_class" ]]
}

kind_cluster_has_ingress_ready_node() {
  local kubectl_cmd="$1"
  local ingress_node
  # deploy/kubernetes/kind-cluster.yaml labels the control-plane node and maps
  # host ports 80/443 at creation; neither can be added to an existing cluster.
  if ! ingress_node="$("$kubectl_cmd" get nodes -l ingress-ready=true -o name 2>/dev/null)"; then
    return 2
  fi
  [[ -n "$ingress_node" ]]
}

kind_cluster_maps_host_ingress_ports() {
  # Exact host ports matter because *.nomos.local uses implicit ports 80/443.
  local host_port port
  for port in 80 443; do
    if ! host_port="$(
      docker inspect --format \
        "{{with index .HostConfig.PortBindings \"${port}/tcp\"}}{{(index . 0).HostPort}}{{end}}" \
        "${KIND_CLUSTER_NAME}-control-plane" 2>/dev/null
    )"; then
      return 2
    fi
    [[ "$host_port" == "$port" ]] || return 1
  done
}

kind_cluster_has_usable_ingress() {
  local kubectl_cmd="$1"
  local status
  if kind_cluster_has_nginx_ingress_class "$kubectl_cmd"; then
    :
  else
    status=$?
    return "$status"
  fi
  if kind_cluster_has_ingress_ready_node "$kubectl_cmd"; then
    :
  else
    status=$?
    return "$status"
  fi
  kind_cluster_maps_host_ingress_ports
}
