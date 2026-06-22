# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


"""Shared helpers for trusted executor implementations."""

from .evidence import (
    ExecutorIdentity,
    ProviderExecutorIdentity,
    certify_execution_result,
    executor_identity_from_signer,
    load_or_create_executor_identity,
)

__all__ = [
    "ExecutorIdentity",
    "ProviderExecutorIdentity",
    "certify_execution_result",
    "executor_identity_from_signer",
    "load_or_create_executor_identity",
]
