# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Strict request and response models for native authorization clients."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class AuthorizeRequest(BaseModel):
    """Exact shell operation submitted by a trusted harness adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    api_version: str = "v1"
    auth_token: str = Field(repr=False)
    agent_id: str = Field(min_length=1)
    council_id: str = Field(min_length=1)
    command: str = Field(min_length=1)
    working_directory: str = Field(min_length=1)
    description: str = "Hermes terminal tool call"
    target_resource: str = ""
    justification: str = ""
    blast_radius: str = ""
    context: str = Field(default="", max_length=32000)


class AuthorizeDecision(BaseModel):
    """Subset of the Council response used at the enforcement boundary."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    api_version: str = "v1"
    council_id: str = ""
    status: str
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
