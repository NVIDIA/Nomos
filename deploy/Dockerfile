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
#   docker run --rm -p 9901:9901 -e ANTHROPIC_API_KEY nomos-council
# ─────────────────────────────────────────────────────────────

FROM python:3.13-slim AS base

# Prevent Python from writing .pyc files and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

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
# Copy only pyproject.toml to cache the dependency install layer.
# We install the heavy deps before copying source so this layer is cached.
COPY pyproject.toml ./
RUN uv pip install --system \
        "cryptography>=42.0.0" \
        "pydantic>=2.0.0" \
        "pydantic-settings>=2.0.0" \
        "pyyaml>=6.0.0" \
        "nvidia-nat>=1.0.0" \
        "nvidia-nat-mcp>=1.0.0" \
        "nvidia-nat-langchain>=1.0.0" \
        "mcp>=1.23.0" \
        "anthropic>=0.30.0" \
        "redis>=5.0.0"

# ── Copy application code ────────────────────────────────────
COPY nomos/ ./nomos/
COPY nomos_runtime/ ./nomos_runtime/
COPY trusted_executor_contract/ ./trusted_executor_contract/
COPY configs/ ./configs/
COPY scripts/agent_accounts.py ./scripts/agent_accounts.py
COPY scripts/councils.py ./scripts/councils.py

# Install the package (registers entry points for NAT plugin discovery)
RUN chmod -R a+rX ./nomos ./nomos_runtime ./trusted_executor_contract ./configs ./scripts && \
    uv pip install --system ".[nat,anthropic,grpc,postgres]"

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
