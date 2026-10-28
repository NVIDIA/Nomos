# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
FROM python:3.13-slim@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91
WORKDIR /opt/nomos
COPY evals/nomos/runtime-requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && mkdir /trial /app /experiment
COPY nomos /opt/nomos/nomos
COPY nomos_runtime /opt/nomos/nomos_runtime
COPY trusted_executor_contract /opt/nomos/trusted_executor_contract
COPY assurance/__init__.py assurance/analysis.py assurance/monitor.py /opt/nomos/assurance/
COPY evals/__init__.py /opt/nomos/evals/__init__.py
COPY evals/nomos/__init__.py evals/nomos/workflow.py evals/nomos/adapter.py evals/nomos/harbor_worker.py /opt/nomos/evals/nomos/
ENV PYTHONPATH=/opt/nomos
