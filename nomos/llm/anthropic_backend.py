"""
Anthropic LLM backend.

Wraps the Anthropic API for use by council member agents.
Requires: pip install anthropic
"""

from typing import Any

from .base import LLMBackend, LLMMessage, LLMResponse, LLMRole


class AnthropicBackend(LLMBackend):
    """Anthropic API backend (Claude Sonnet, Opus, etc.)."""

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-20250514",
        base_url: str | None = None,
    ):
        try:
            from anthropic import AsyncAnthropic
        except ImportError as err:
            raise ImportError(
                "anthropic package required. Install with: pip install anthropic"
            ) from err

        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url

        self._client = AsyncAnthropic(**kwargs)
        self._model = model

    async def complete(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.0,
        max_tokens: int = 1024,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        # Anthropic uses a separate system parameter
        system_content = ""
        conversation: list[dict[str, str]] = []

        for msg in messages:
            if msg.role == LLMRole.SYSTEM:
                system_content = msg.content
            else:
                conversation.append({"role": msg.role.value, "content": msg.content})

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": conversation,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if system_content:
            kwargs["system"] = system_content

        response = await self._client.messages.create(**kwargs)

        content = ""
        for block in response.content:
            if hasattr(block, "text"):
                content += block.text

        usage = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }

        return LLMResponse(
            content=content,
            model=response.model,
            usage=usage,
            raw={"id": response.id, "stop_reason": response.stop_reason},
        )

    def provider_name(self) -> str:
        return "anthropic"
