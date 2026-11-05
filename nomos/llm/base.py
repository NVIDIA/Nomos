# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Abstract LLM backend interface.

Provides a unified interface for calling different LLM providers
(OpenAI, Anthropic, NVIDIA NIM) used by council member agents.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class LLMRateLimitError(Exception):
    """The provider rejected the request due to rate limiting (HTTP 429)."""

    def __init__(self, message: str = "rate limited", retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class LLMTimeoutError(Exception):
    """The provider request exceeded the configured timeout."""


class LLMRole(StrEnum):
    """Message roles in a conversation."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class LLMMessage(BaseModel):
    """A single message in a conversation."""

    role: LLMRole
    content: str


class LLMAttempt(BaseModel):
    """Sanitized metadata for one invocation, including endpoint queue time."""

    backend_id: str
    provider: str
    configured_model: str
    returned_model: str | None = None
    outcome: Literal[
        "success",
        "timeout",
        "rate_limit",
        "unavailable",
        "invalid_response",
        "terminal_error",
        "cancelled",
    ]
    duration_ms: float


class LLMExecution(BaseModel):
    """A caller-owned snapshot; never a model's claim about its provider."""

    requested_backend: str
    selected_backend: str | None = None
    attempts: list[LLMAttempt] = Field(default_factory=list)


class LLMResponse(BaseModel):
    """Response from an LLM backend."""

    execution: LLMExecution | None = None
    content: str
    model: str = ""
    usage: dict[str, int] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)
    # Set by LimitedBackend: {"queue_wait_ms": float, "inference_ms": float}.
    # Lets telemetry separate time spent waiting for a concurrency slot from
    # time spent in the provider call.
    timing: dict[str, float] = Field(default_factory=dict)


class LLMBackend(ABC):
    """
    Abstract interface for LLM providers.

    Council member agents use this to evaluate intents.
    Each backend wraps a specific provider (OpenAI, Anthropic, NIM).
    """

    @abstractmethod
    async def complete(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.0,
        max_tokens: int = 1024,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """
        Send messages to the LLM and get a response.

        Parameters
        ----------
        messages : List[LLMMessage]
            The conversation history to send to the model.
        temperature : float, optional
            Sampling temperature between 0.0 and 2.0, by default 0.0.
        max_tokens : int, optional
            Maximum number of tokens to generate, by default 1024.
        response_format : Optional[Dict[str, Any]], optional
            Optional format constraint (e.g., {"type": "json_object"}), by default None.

        Returns
        -------
        LLMResponse
            The structured response from the model.
        """
        ...

    @abstractmethod
    def provider_name(self) -> str:
        """
        Return the name of this provider.

        Returns
        -------
        str
            The provider name identifier.
        """
        ...


@dataclass(frozen=True)
class LLMCallCandidate:
    """One explicitly approved adapter in a finite, immutable call route."""

    backend_id: str
    provider: str
    model: str
    request_timeout_seconds: float
    backend: LLMBackend
