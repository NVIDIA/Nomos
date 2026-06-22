# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


"""Open Policy Agent integration for front-door intent filtering."""

from .client import (
    OPAEvaluationError,
    OPAHttpClient,
    OPAPolicyDecision,
    build_intent_filter_input,
    parse_opa_decision,
)

__all__ = [
    "OPAEvaluationError",
    "OPAHttpClient",
    "OPAPolicyDecision",
    "build_intent_filter_input",
    "parse_opa_decision",
]
