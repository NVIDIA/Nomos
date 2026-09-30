# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Dependency-light client contracts for native Nomos authorization adapters."""

from .client import AuthorizationClient
from .models import (
    AuthorizeDecision,
    AuthorizeRequest,
    ConsumeDecision,
    ConsumeRequest,
)

__all__ = [
    "AuthorizationClient",
    "AuthorizeDecision",
    "AuthorizeRequest",
    "ConsumeDecision",
    "ConsumeRequest",
]
