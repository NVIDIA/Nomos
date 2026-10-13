# SPDX-License-Identifier: Apache-2.0
# DL3022 does not recognize BuildKit named contexts; each is verified by the deploy script.
# Named build contexts must be clean, pinned upstream repositories (see deploy script).
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app:/opt/compass/src:/opt/compass/packages/trace-ingest/src \
    EVAL_AUTHOR_ROOT=/opt/eval-author
WORKDIR /app
COPY deploy/assurance/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock \
    && useradd --uid 10001 --create-home assurance \
    && mkdir /state && chown assurance:assurance /state
# hadolint ignore=DL3022
COPY --from=compass /src/insight_agent /opt/compass/src/insight_agent
# hadolint ignore=DL3022
COPY --from=compass /packages/trace-ingest /opt/compass/packages/trace-ingest
# hadolint ignore=DL3022
COPY --from=compass /LICENSE /opt/compass/LICENSE
# hadolint ignore=DL3022
COPY --from=evalauthor /skills/eval-author-audit /opt/eval-author/skills/eval-author-audit
# hadolint ignore=DL3022
COPY --from=evalauthor /LICENSE /opt/eval-author/LICENSE
COPY nomos /app/nomos
COPY trusted_executor_contract /app/trusted_executor_contract
COPY nomos_runtime /app/nomos_runtime
COPY assurance /app/assurance
USER 10001:10001
EXPOSE 8080
CMD ["uvicorn", "assurance.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
