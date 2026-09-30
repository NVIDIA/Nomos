# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Transport-neutral client interface for native authorization adapters."""

from __future__ import annotations

from typing import Protocol

from .models import AuthorizeDecision, AuthorizeRequest, ConsumeDecision, ConsumeRequest


class AuthorizationClient(Protocol):
    """The two operations required immediately before native dispatch."""

    async def authorize(self, request: AuthorizeRequest) -> AuthorizeDecision:
        """Request a Council decision and signed writ."""

    async def consume(self, request: ConsumeRequest) -> ConsumeDecision:
        """Atomically consume a locally verified writ."""
