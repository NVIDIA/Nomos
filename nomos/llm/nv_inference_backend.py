# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
NVIDIA Cloud Functions (NVCF) inference backend.

Targets https://inference-api.nvidia.com/v1/ — distinct from the NIM catalog
endpoint at integrate.api.nvidia.com used by NIMBackend. Model ids use the
nvcf/ prefix (e.g. nvcf/meta/llama-3.1-70b-instruct).

OpenAI-compatible API; requires: pip install openai
"""

from __future__ import annotations

from typing import Any

from .base import LLMBackend, LLMMessage, LLMResponse

NO_TEMPERATURE_MODELS = frozenset(
    {
        "aws/anthropic/bedrock-claude-opus-4-7",
        "openai/openai/gpt-5.5",
    }
)


class NVInferenceBackend(LLMBackend):
    """NVIDIA Cloud Functions inference backend (OpenAI-compatible API)."""

    DEFAULT_BASE_URL = "https://inference-api.nvidia.com"

    def __init__(
        self,
        api_key: str,
        model: str = "azure/anthropic/claude-sonnet-4-6",
        base_url: str | None = None,
        top_p: float | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
    ):
        try:
            from openai import AsyncOpenAI
        except ImportError as err:
            raise ImportError(
                "openai package required for nv_inference backend. Install with: pip install openai"
            ) from err

        kwargs: dict[str, Any] = {
            "api_key": api_key,
            "base_url": base_url or self.DEFAULT_BASE_URL,
        }
        if timeout is not None:
            kwargs["timeout"] = timeout
        if max_retries is not None:
            kwargs["max_retries"] = max_retries

        self._client = AsyncOpenAI(**kwargs)
        self._model = model
        self._top_p = top_p
        self._send_temperature = model not in NO_TEMPERATURE_MODELS

    async def complete(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.0,
        max_tokens: int = 1024,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": m.role.value, "content": m.content} for m in messages],
            "max_tokens": max_tokens,
        }
        if self._send_temperature:
            kwargs["temperature"] = temperature
        if self._top_p is not None:
            # Some NVCF-routed models (notably Bedrock Claude) reject temperature +
            # top_p together. Only send top_p when the caller explicitly set it.
            kwargs["top_p"] = self._top_p
        if response_format:
            kwargs["response_format"] = response_format

        response = await self._client.chat.completions.create(**kwargs)

        choice = response.choices[0]
        usage = {}
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }

        return LLMResponse(
            content=choice.message.content or "",
            model=response.model,
            usage=usage,
            raw={"id": response.id, "finish_reason": choice.finish_reason},
        )

    def provider_name(self) -> str:
        return "nv_inference"
