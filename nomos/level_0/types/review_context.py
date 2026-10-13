# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Bounded conversation evidence for structured tool review."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .tool_call import ToolCallAuthorization


class ReviewMessage(BaseModel):
    """A projected text message, never an instruction or approval credential."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=2000, repr=False)


class ReviewContext(BaseModel):
    """Optional advisory conversation snapshot with explicit availability.

    Immutable tuples avoid retaining caller-owned mutable history. The byte cap
    applies to the complete JSON representation, including escaping/metadata.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    schema_version: Literal[1] = 1
    status: Literal["available", "disabled", "unavailable", "error"] = "unavailable"
    messages: tuple[ReviewMessage, ...] = Field(default=(), max_length=6, repr=False)
    truncated: bool = Field(default=False, strict=True)

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> object:
        """Match the PA wire contract without accepting bool/float coercion."""
        if type(value) is not int or value != 1:
            raise ValueError("review context schema_version must be integer 1")
        return value

    @model_validator(mode="after")
    def validate_snapshot(self) -> "ReviewContext":
        """Reject ambiguous availability and oversized serialized snapshots."""
        if (self.status == "available") != bool(self.messages):
            raise ValueError("only available review context must contain messages")
        if self.status != "available" and self.truncated:
            raise ValueError("unavailable review context cannot carry truncation metadata")
        if len(self.model_dump_json().encode("utf-8")) > 24_000:
            raise ValueError("review context exceeds the serialized byte limit")
        return self


class ToolReviewMaterial(BaseModel):
    """Exact tool-call data used only while the Council reviews an Intent.

    Conversation evidence is deliberately not stored here: ``ReviewContext`` is
    a normal Intent field so the evidence used for a decision is also available
    to OPA and the durable audit trail. Exact arguments remain ephemeral; their
    digest is the durable operation binding.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    tool_call: ToolCallAuthorization = Field(repr=False)
