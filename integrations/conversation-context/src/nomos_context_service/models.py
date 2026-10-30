# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Version-one request contracts; context is untrusted publisher-reported data."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]
# Provider/supervisor identifiers are opaque; do not require our generated-ID alphabet.
OpaqueIdentifier = Annotated[str, Field(min_length=1, max_length=256)]


class ApiModel(BaseModel):
    """Reject unknown fields and implicit conversions at the API boundary."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class SnapshotInput(ApiModel):
    """Selected context plus an immutable retry deadline, never an authorization."""

    sandbox_name: Identifier
    snapshot_id: Identifier
    model_attempt_id: Identifier
    model_name: Annotated[str, Field(min_length=1, max_length=256)]
    not_after: AwareDatetime
    context: dict[str, JsonValue]


class ToolRecordInput(ApiModel):
    """One immutable lifecycle event; each state transition needs a new record ID."""

    sandbox_name: Identifier
    record_id: Identifier
    snapshot_id: Identifier
    tool_call_id: OpaqueIdentifier | None = None
    tool_name: Annotated[str, Field(min_length=1, max_length=128)]
    state: Literal[
        "requested", "denied", "execution_pending", "completed", "failed", "execution_unknown"
    ]
    arguments: dict[str, JsonValue] | None = None


class LookupInput(ApiModel):
    """Name-based lookup; observed IDs are annotations, not publisher attestation."""

    sandbox_name: Identifier
    observed_sandbox_id: OpaqueIdentifier | None = None
    http_request_id: OpaqueIdentifier | None = None
    include_content: bool = False
