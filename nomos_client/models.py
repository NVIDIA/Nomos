# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Strict request and response models for native authorization clients."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nomos.level_0.types.review_context import ReviewContext
from nomos.level_0.types.tool_call import ToolCallAuthorization


class AuthorizeRequest(BaseModel):
    """Exact shell or structured tool operation submitted for authorization."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    api_version: str = "v1"
    auth_token: str = Field(repr=False)
    agent_id: str = Field(min_length=1)
    council_id: str = Field(min_length=1)
    command: str = ""
    working_directory: str = ""
    tool_call: ToolCallAuthorization | None = None
    review_context: ReviewContext | None = None
    description: str = "Native operation authorization"
    target_resource: str = ""
    justification: str = ""
    blast_radius: str = ""
    context: str = Field(default="", max_length=32000)

    @model_validator(mode="after")
    def validate_operation(self) -> AuthorizeRequest:
        if self.tool_call is None and self.review_context is not None:
            raise ValueError("review_context requires a tool-call operation")
        if self.tool_call is None and (not self.command or not self.working_directory):
            raise ValueError("shell authorization requires command and working_directory")
        if self.tool_call is not None and (self.command or self.working_directory or self.context):
            raise ValueError("tool-call authorization cannot include shell or context fields")
        return self


class AuthorizeDecision(BaseModel):
    """Subset of the Council response used at the enforcement boundary."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    api_version: str = "v1"
    council_id: str = ""
    status: str
    denial_category: (
        Literal["review_denied", "permission_denied", "policy_denied", "review_unavailable"] | None
    ) = None
    writ: str | None = None
    writ_id: str = ""
    decision_id: str = ""
    guidance: list[str] = Field(default_factory=list)
    error: str = ""
    retryable: bool = False


class ConsumeRequest(BaseModel):
    """Authenticated request to claim a verified writ's nonce."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    api_version: str = "v1"
    council_id: str = Field(min_length=1)
    writ_id: str = Field(min_length=1)
    nonce: str = Field(pattern=r"^[0-9a-fA-F]{64}$", repr=False)
    auth_token: str = Field(repr=False)


class ConsumeDecision(BaseModel):
    """Atomic claim result returned by the Council service."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    api_version: str = "v1"
    council_id: str = ""
    writ_id: str = ""
    status: str
    consumed: bool = False
    error: str = ""
