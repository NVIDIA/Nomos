# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Council MCP monitor: transparent reverse proxy + observability dashboard.

The monitor binds the externally visible MCP port and forwards traffic to the
council. It records per-request timing and source attribution so the dashboard
can report inflight count, latency percentiles, and throughput across all
ingress paths (direct MCP clients, the gRPC bridge, the demo).

The monitor does not store auth tokens, writ payloads, command text, file
contents, or environment values. It extracts sanitized request/decision
summaries for observability only; council remains the sole authorization
authority.
"""
