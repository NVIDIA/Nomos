# SPDX-License-Identifier: Apache-2.0
# The lock includes Compass's immutable HTTPS Git revision and all runtime dependencies.
FROM python:3.13-slim AS builder
RUN apt-get update && apt-get install --yes --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY deploy/assurance/requirements.lock /tmp/requirements.lock
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.lock

FROM python:3.13-slim AS assurance
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app PATH=/opt/venv/bin:$PATH \
    LITELLM_LOCAL_MODEL_COST_MAP=True \
    EVAL_AUTHOR_ROOT=/opt/eval-author
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
RUN useradd --uid 10001 --create-home assurance \
    && mkdir /state && chown assurance:assurance /state
COPY nomos /app/nomos
COPY trusted_executor_contract /app/trusted_executor_contract
COPY nomos_runtime /app/nomos_runtime
COPY assurance /app/assurance
USER 10001:10001
EXPOSE 8080
CMD ["uvicorn", "assurance.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]

# Optional target: requires a clean pinned evalauthor named context supplied by
# scripts/assurance_poc.py --with-export. ETHOS authoring skills are never copied.
FROM assurance AS assurance-export
# hadolint ignore=DL3022
COPY --from=evalauthor /skills/eval-author-audit /opt/eval-author/skills/eval-author-audit
# hadolint ignore=DL3022
COPY --from=evalauthor /LICENSE /opt/eval-author/LICENSE

# The default monitoring image contains no Eval Author runtime tooling.
FROM assurance AS runtime
