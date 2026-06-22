# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


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
