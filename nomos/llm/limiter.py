"""
Shared concurrency limiting for LLM endpoints.

The inference provider is a fixed external budget: multiple backend names
(and multiple councils) in one process may all target the same endpoint, so
in-flight requests must be capped per (provider, base_url) — not per backend
instance. `LimiterPool` owns that mapping; `LimitedBackend` wraps a concrete
backend so every `complete()` call passes through the shared limiter and
provider failures surface as normalized, retry-classifiable errors.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from .base import (
    LLMBackend,
    LLMMessage,
    LLMRateLimitError,
    LLMResponse,
    LLMTimeoutError,
)

logger = logging.getLogger(__name__)

# Seed for the latency EWMA before any sample arrives, so estimated waits
# reported during a cold-start burst are plausible rather than zero.
_DEFAULT_LATENCY_SECONDS = 3.0
_EWMA_ALPHA = 0.2


class EndpointLimiter:
    """Caps in-flight requests to one inference endpoint."""

    def __init__(self, max_concurrent: int, overload_latency_threshold: float = 0.0):
        self.max_concurrent = max_concurrent
        self.overload_latency_threshold = overload_latency_threshold
        self.waiting = 0
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._latency_ewma = _DEFAULT_LATENCY_SECONDS

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[None]:
        self.waiting += 1
        try:
            await self._semaphore.acquire()
        finally:
            self.waiting -= 1
        try:
            yield
        finally:
            self._semaphore.release()

    def record_latency(self, seconds: float) -> None:
        self._latency_ewma += _EWMA_ALPHA * (seconds - self._latency_ewma)

    def estimated_wait_seconds(self) -> float:
        """Rough queue-drain estimate for callers deciding whether to shed."""
        return (self.waiting / self.max_concurrent) * self._latency_ewma

    def projected_response_seconds(self) -> float:
        """Expected response time for a NEW arrival: queue drain + one call."""
        return self.estimated_wait_seconds() + self._latency_ewma

    @property
    def overloaded(self) -> bool:
        """Queue depth never matters by itself — only its latency implication.

        Sheds when a new request's projected response time exceeds the
        configured threshold; 0 disables shedding entirely.
        """
        return 0 < self.overload_latency_threshold <= self.projected_response_seconds()


class LimiterPool:
    """Endpoint-keyed registry of limiters, shared across councils in a process."""

    def __init__(self) -> None:
        self._limiters: dict[tuple[str, str], EndpointLimiter] = {}

    def get(
        self,
        provider: str,
        base_url: str,
        *,
        max_concurrent: int,
        overload_latency_threshold: float,
    ) -> EndpointLimiter:
        key = (provider, base_url)
        limiter = self._limiters.get(key)
        if limiter is None:
            limiter = EndpointLimiter(max_concurrent, overload_latency_threshold)
            self._limiters[key] = limiter
            return limiter
        if (
            limiter.max_concurrent != max_concurrent
            or limiter.overload_latency_threshold != overload_latency_threshold
        ):
            logger.warning(
                "endpoint %s/%s already limited to max_concurrent=%d "
                "overload_latency_threshold=%.1fs; ignoring conflicting config "
                "(max_concurrent=%d, overload_latency_threshold=%.1fs)",
                provider,
                base_url,
                limiter.max_concurrent,
                limiter.overload_latency_threshold,
                max_concurrent,
                overload_latency_threshold,
            )
        return limiter


def _status_code(exc: Exception) -> int | None:
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    return code if isinstance(code, int) else None


def _retry_after(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        raw = headers.get("retry-after")
    except Exception:
        return None
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def _is_timeout(exc: Exception) -> bool:
    # Match SDK timeout classes (openai.APITimeoutError, anthropic.APITimeoutError,
    # httpx.TimeoutException ancestors) by name so no SDK import is required.
    return any("Timeout" in cls.__name__ for cls in type(exc).__mro__)


class LimitedBackend(LLMBackend):
    """Wraps a backend so completions share an endpoint limiter."""

    def __init__(self, inner: LLMBackend, limiter: EndpointLimiter):
        self._inner = inner
        self.limiter = limiter

    async def complete(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.0,
        max_tokens: int = 1024,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        async with self.limiter.acquire():
            loop = asyncio.get_running_loop()
            started = loop.time()
            try:
                response = await self._inner.complete(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                )
            except Exception as exc:
                if _status_code(exc) == 429:
                    raise LLMRateLimitError(str(exc), retry_after=_retry_after(exc)) from exc
                if _is_timeout(exc):
                    raise LLMTimeoutError(str(exc)) from exc
                raise
            self.limiter.record_latency(loop.time() - started)
            return response

    def provider_name(self) -> str:
        return self._inner.provider_name()
