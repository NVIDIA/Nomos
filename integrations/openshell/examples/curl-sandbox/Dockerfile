# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# Ubuntu 24.04 workload tools; OpenShell runtime images are managed separately.
FROM nvcr.io/nvidia/base/ubuntu:24.04@sha256:7e0f726d635d440d28a7f18e6498d3bdb76c7038bd4c90a090c21eb7c9f40686
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      curl ca-certificates libgcc-s1 && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*
