"""
Abstract LLM backend interface.

Provides a unified interface for calling different LLM providers
(OpenAI, Anthropic, NVIDIA NIM) used by council member agents.
"""

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any

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


class LLMResponse(BaseModel):
    """Response from an LLM backend."""

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
