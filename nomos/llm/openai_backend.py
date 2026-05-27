"""
OpenAI LLM backend.

Wraps the OpenAI API for use by council member agents.
Requires: pip install openai
"""

from typing import Any

from .base import LLMBackend, LLMMessage, LLMResponse


class OpenAIBackend(LLMBackend):
    """OpenAI API backend (GPT-4o, GPT-4, etc.)."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o",
        base_url: str | None = None,
    ):
        try:
            from openai import AsyncOpenAI
        except ImportError as err:
            raise ImportError("openai package required. Install with: pip install openai") from err

        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url

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
        return "openai"
