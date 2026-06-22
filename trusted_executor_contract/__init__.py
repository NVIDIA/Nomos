# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


"""Trusted executor HTTP contract.

This package is intentionally small and dependency-light so third-party
executor services can import the same models used by the example executor
without depending on the council runtime.
"""

from .models import (
    AGENT_ID_HEADER,
    CONTRACT_VERSION,
    EXECUTE_ENDPOINT,
    HEALTH_ENDPOINT,
    LEGACY_EXECUTE_ENDPOINT,
    LEGACY_HEALTH_ENDPOINT,
    WELL_KNOWN_ENDPOINT,
    ContractDescriptor,
    ExecutedCommandEvidence,
    ExecuteWritRequest,
    ExecutionAttachmentEvidence,
    ExecutionContext,
    ExecutionEvidence,
    ExecutionEvidencePayload,
    ExecutionResult,
    ExecutionResultEvidence,
    ExecutorCapabilities,
    ExecutorHealth,
    PackagedFile,
    TrustedExecutor,
    VoteSummary,
    WritOfExecution,
    build_agent_binding_error,
    validate_agent_binding,
)

__all__ = [
    "AGENT_ID_HEADER",
    "CONTRACT_VERSION",
    "EXECUTE_ENDPOINT",
    "HEALTH_ENDPOINT",
    "LEGACY_EXECUTE_ENDPOINT",
    "LEGACY_HEALTH_ENDPOINT",
    "WELL_KNOWN_ENDPOINT",
    "ContractDescriptor",
    "ExecuteWritRequest",
    "ExecutedCommandEvidence",
    "ExecutionAttachmentEvidence",
    "ExecutionContext",
    "ExecutionEvidence",
    "ExecutionEvidencePayload",
    "ExecutionResult",
    "ExecutionResultEvidence",
    "ExecutorCapabilities",
    "ExecutorHealth",
    "PackagedFile",
    "TrustedExecutor",
    "VoteSummary",
    "WritOfExecution",
    "build_agent_binding_error",
    "validate_agent_binding",
]
