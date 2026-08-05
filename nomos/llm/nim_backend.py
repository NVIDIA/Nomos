# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
NVIDIA NIM LLM backend.

Wraps NVIDIA NIM API (OpenAI-compatible) for use by council member agents.
Uses the OpenAI client pointed at NVIDIA's endpoint.
Requires: pip install openai
"""

from typing import Any

from .base import LLMBackend, LLMMessage, LLMResponse


class NIMBackend(LLMBackend):
    """NVIDIA NIM backend (OpenAI-compatible API)."""

    DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"

    def __init__(
        self,
        api_key: str,
        model: str = "nvidia/nemotron-3-nano-30b-a3b",
        base_url: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
    ):
        try:
            from openai import AsyncOpenAI
        except ImportError as err:
            raise ImportError(
                "openai package required for NIM backend. Install with: pip install openai"
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
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
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
        return "nim"
