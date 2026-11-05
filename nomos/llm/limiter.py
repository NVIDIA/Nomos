# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

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
import errno
import importlib
import logging
import socket
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

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

    def __init__(
        self,
        max_concurrent: int,
        overload_latency_threshold: float = 0.0,
        *,
        provider: str = "",
        base_url: str = "",
    ):
        self.max_concurrent = max_concurrent
        self.overload_latency_threshold = overload_latency_threshold
        self.provider = provider
        self.base_url = base_url
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

    def gauge(self) -> dict[str, Any]:
        """Live pressure snapshot for telemetry/dashboards."""
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "waiting": self.waiting,
            "max_concurrent": self.max_concurrent,
            "latency_ewma_ms": round(self._latency_ewma * 1000, 1),
            "projected_call_ms": round(self.projected_response_seconds() * 1000, 1),
        }


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
            limiter = EndpointLimiter(
                max_concurrent,
                overload_latency_threshold,
                provider=provider,
                base_url=base_url,
            )
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


def _status_code(exc: BaseException) -> int | None:
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


def classify_availability_error(
    error: Exception,
) -> Literal["timeout", "rate_limit", "unavailable"] | None:
    """Allow only known transient statuses and recognized SDK transport failures.

    Inspect the original causes before normalized wrappers: auth/configuration
    failures must win even when a wrapper labels them as timeouts or throttling.
    Optional SDK imports stay at this adapter boundary.
    """
    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and all(current is not item for item in chain):
        chain.append(current)
        current = current.__cause__ or current.__context__
    statuses = [_status_code(item) for item in chain if isinstance(item, Exception)]
    if any(code is not None and code not in {408, 429, 500, 502, 503, 504} for code in statuses):
        return None

    import httpx

    if any(
        isinstance(
            item,
            (
                ssl.SSLError,
                httpx.InvalidURL,
                httpx.UnsupportedProtocol,
                httpx.ProxyError,
                ValueError,
            ),
        )
        for item in chain
    ):
        return None
    sdk_status: list[type[Exception]] = []
    sdk_timeout: list[type[Exception]] = []
    sdk_connection: list[type[Exception]] = []
    for module_name in ("openai", "anthropic"):
        try:
            sdk = importlib.import_module(module_name)
        except ImportError:
            continue
        sdk_status.append(sdk.APIStatusError)
        sdk_timeout.append(sdk.APITimeoutError)
        sdk_connection.append(sdk.APIConnectionError)
    for item in chain:
        if isinstance(item, tuple(sdk_status)):
            code = _status_code(item)
            if code == 429:
                return "rate_limit"
            if code == 408:
                return "timeout"
            if code in {500, 502, 503, 504}:
                return "unavailable"
            return None
    if isinstance(error, (LLMTimeoutError, LLMRateLimitError)) and all(
        isinstance(item, (LLMTimeoutError, LLMRateLimitError)) for item in chain
    ):
        return "timeout" if isinstance(error, LLMTimeoutError) else "rate_limit"
    if any(isinstance(item, tuple(sdk_timeout)) for item in chain):
        return "timeout"
    if isinstance(error, LLMTimeoutError) and isinstance(error.__cause__, TimeoutError):
        return "timeout"  # our outer queue-plus-inference deadline
    if any(isinstance(item, tuple(sdk_connection)) for item in chain):
        network_errnos = {
            errno.ECONNREFUSED,
            errno.ECONNRESET,
            errno.ECONNABORTED,
            errno.ENETUNREACH,
            errno.EHOSTUNREACH,
            errno.ETIMEDOUT,
            errno.EPIPE,
        }
        if any(
            (isinstance(item, OSError) and item.errno in network_errnos)
            or (isinstance(item, socket.gaierror) and item.errno == socket.EAI_AGAIN)
            or isinstance(item, (httpx.TimeoutException, httpx.RemoteProtocolError))
            for item in chain
        ):
            return "unavailable"
    return None


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
        loop = asyncio.get_running_loop()
        enqueued = loop.time()
        async with self.limiter.acquire():
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
            ended = loop.time()
            self.limiter.record_latency(ended - started)
            response.timing = {
                "queue_wait_ms": round((started - enqueued) * 1000, 1),
                "inference_ms": round((ended - started) * 1000, 1),
            }
            return response

    def provider_name(self) -> str:
        return self._inner.provider_name()
