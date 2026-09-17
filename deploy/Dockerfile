# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# ─────────────────────────────────────────────────────────────
# Nomos Council MCP Server
#
# Runs the council as a containerized MCP endpoint.
# Commands approved by the council execute INSIDE this container,
# so the host system is never directly exposed.
#
# Build:
#   docker build -f deploy/Dockerfile -t nomos-council .
#
# Run:
#   docker run --rm -p 9901:9901 --env-file deploy/.env nomos-council
# ─────────────────────────────────────────────────────────────

FROM python:3.13-slim AS base

# Prevent Python from writing .pyc files and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# ── Install system deps ──────────────────────────────────────
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        git \
    && rm -rf /var/lib/apt/lists/*

# ── Install uv ───────────────────────────────────────────────
RUN pip install --no-cache-dir uv

# ── Install deps first (cache layer) ─────────────────────────
# Copy only dependency metadata to cache the dependency install layer.
# We install the heavy deps before copying source so this layer is cached.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project \
        --extra nat --extra anthropic --extra openai --extra grpc --extra postgres

# ── Copy application code ────────────────────────────────────
COPY nomos/ ./nomos/
COPY nomos_runtime/ ./nomos_runtime/
COPY trusted_executor_contract/ ./trusted_executor_contract/
COPY configs/ ./configs/
COPY scripts/agent_accounts.py ./scripts/agent_accounts.py
COPY scripts/councils.py ./scripts/councils.py
# pyproject declares `readme = "README.md"`, so the package build below reads it.
COPY README.md ./

# Install the package (registers entry points for NAT plugin discovery)
RUN chmod -R a+rX ./nomos ./nomos_runtime ./trusted_executor_contract ./configs ./scripts && \
    uv sync --frozen --no-dev \
        --extra nat --extra anthropic --extra openai --extra grpc --extra postgres

# Block incompatible dependency overrides and broken runtime crypto integrations.
RUN python - <<'PY'
import subprocess
import sys

pip_check = subprocess.run([sys.executable, "-m", "pip", "check"], check=False)

from mcp import ClientSession
from nat.builder.builder import Builder
from nomos.level_0.crypto import KeyPair, Signer, Verifier

payload = b"nomos-image-signing-validation"
key_pair = KeyPair.generate(key_id="image-build")
verifier = Verifier()
verifier.register_key_pair(key_pair)
signature = Signer(key_pair).sign(payload)
if not verifier.verify(payload, signature):
    raise RuntimeError("Ed25519 signing verification failed")
if pip_check.returncode:
    raise SystemExit("pip check failed")
PY

# ── Create non-root user for execution sandbox ───────────────
RUN useradd --create-home --shell /bin/bash council && \
    mkdir -p /app/workspace /app/keys /app/public-keys /app/ledger && \
    chown -R council:council /app/workspace /app/keys /app/public-keys /app/ledger

# ── Runtime config ───────────────────────────────────────────
EXPOSE 9901 9904

# The workspace directory is where approved commands execute.
# It's the only writable area — the rest of the container is read-only.
ENV COUNCIL_WORKSPACE=/app/workspace

USER council

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -sf http://localhost:9901/debug/tools/list || exit 1

ENTRYPOINT ["nat", "mcp", "serve", "--config_file", "configs/council_nat_workflow.yaml", "--host", "0.0.0.0"]
