# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

# NeMo Guardrails prose-screen sidecar. Serves one rails profile per
# subdirectory of /configs; the council selects a profile per request with
# guardrails.config_id. V1 profiles use only local, model-free rails
# (YARA injection detection, context-bloat thresholds) — no GPU, no API keys.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "nemoguardrails[server]==0.24.0" yara-python \
    && useradd --create-home --uid 10001 guardrails
USER guardrails

EXPOSE 8000
ENTRYPOINT ["nemoguardrails", "server", "--config", "/configs", "--port", "8000"]
