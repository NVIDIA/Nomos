"""Tests for shared LLM endpoint concurrency limiting and retry behavior."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from nomos.config.settings import CouncilMemberConfig, LLMBackendConfig
from nomos.llm.base import (
    LLMBackend,
    LLMMessage,
    LLMRateLimitError,
    LLMResponse,
    LLMRole,
    LLMTimeoutError,
)
from nomos.llm.council_agent import CouncilMemberAgent, create_backend
from nomos.llm.limiter import EndpointLimiter, LimitedBackend, LimiterPool

APPROVE_JSON = json.dumps({"decision": "approve", "reasoning": "Safe operation."})

MESSAGES = [LLMMessage(role=LLMRole.USER, content="hello")]


class _BlockingBackend(LLMBackend):
    """Counts concurrent completions; callers release via the event."""

    def __init__(self, release: asyncio.Event):
        self._release = release
        self.concurrent = 0
        self.peak_concurrent = 0

    async def complete(self, messages, temperature=0.0, max_tokens=1024, response_format=None):
        self.concurrent += 1
        self.peak_concurrent = max(self.peak_concurrent, self.concurrent)
        try:
            await self._release.wait()
        finally:
            self.concurrent -= 1
        return LLMResponse(content=APPROVE_JSON, model="mock")

    def provider_name(self):
        return "mock"


class _ErrorBackend(LLMBackend):
    """Raises the given exceptions in sequence, then returns approve."""

    def __init__(self, errors: list[Exception]):
        self._errors = errors
        self.calls = 0

    async def complete(self, messages, temperature=0.0, max_tokens=1024, response_format=None):
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)
        return LLMResponse(content=APPROVE_JSON, model="mock")

    def provider_name(self):
        return "mock"


def _member_agent(backend: LLMBackend, **kwargs) -> CouncilMemberAgent:
    config = CouncilMemberConfig(id="limit-agent", backend="mock", priorities=["Test limits"])
    return CouncilMemberAgent(member_config=config, backend=backend, **kwargs)


@pytest.fixture
def test_intent():
    from nomos.level_0.types import Intent

    return Intent.create(
        agent_id="test-agent",
        description="List files",
        target_resource="/workspace",
        command="ls -la",
        justification="Routine inspection",
    )


class TestEndpointLimiter:
    @pytest.mark.asyncio
    async def test_bounds_concurrency(self):
        release = asyncio.Event()
        inner = _BlockingBackend(release)
        limiter = EndpointLimiter(max_concurrent=2)
        backend = LimitedBackend(inner, limiter)

        tasks = [asyncio.create_task(backend.complete(MESSAGES)) for _ in range(6)]
        await asyncio.sleep(0.05)
        assert inner.peak_concurrent == 2
        assert limiter.waiting == 4

        release.set()
        await asyncio.gather(*tasks)
        assert inner.peak_concurrent == 2
        assert limiter.waiting == 0

    @pytest.mark.asyncio
    async def test_overloaded_reflects_projected_latency(self):
        release = asyncio.Event()
        # EWMA seeds at 3.0s; with 2 queued behind 1 in flight a new arrival
        # projects (2/1)*3 + 3 = 9s — past an 8s threshold.
        backend = LimitedBackend(
            _BlockingBackend(release),
            EndpointLimiter(max_concurrent=1, overload_latency_threshold=8.0),
        )

        assert not backend.limiter.overloaded
        tasks = [asyncio.create_task(backend.complete(MESSAGES)) for _ in range(3)]
        await asyncio.sleep(0.05)
        assert backend.limiter.overloaded

        release.set()
        await asyncio.gather(*tasks)
        # Queue drained (and fast completions only lower the EWMA) → recovered.
        assert not backend.limiter.overloaded

    def test_queue_depth_alone_never_sheds(self):
        # 119 queued at 3s EWMA on 4-concurrent projects ~92s — under a 120s
        # threshold the queue is deep but acceptance continues.
        limiter = EndpointLimiter(max_concurrent=4, overload_latency_threshold=120.0)
        limiter.waiting = 119
        assert not limiter.overloaded
        limiter.waiting = 160  # projects (160/4)*3 + 3 = 123s -> past threshold
        assert limiter.overloaded

    def test_estimated_wait_uses_recorded_latency(self):
        limiter = EndpointLimiter(max_concurrent=2)
        for _ in range(50):
            limiter.record_latency(4.0)
        limiter.waiting = 4
        assert limiter.estimated_wait_seconds() == pytest.approx(8.0, rel=0.05)
        # Projected response for a NEW arrival adds its own service time.
        assert limiter.projected_response_seconds() == pytest.approx(12.0, rel=0.05)

    def test_zero_threshold_disables_shedding(self):
        limiter = EndpointLimiter(max_concurrent=1, overload_latency_threshold=0)
        limiter.waiting = 100
        assert not limiter.overloaded


class TestTimingCapture:
    @pytest.mark.asyncio
    async def test_response_timing_splits_queue_and_inference(self):
        release = asyncio.Event()
        backend = LimitedBackend(
            _BlockingBackend(release),
            EndpointLimiter(max_concurrent=1, overload_latency_threshold=0),
        )
        first = asyncio.create_task(backend.complete(MESSAGES))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(backend.complete(MESSAGES))
        await asyncio.sleep(0.05)
        release.set()
        r1, r2 = await asyncio.gather(first, second)
        for r in (r1, r2):
            assert set(r.timing) == {"queue_wait_ms", "inference_ms"}
        # The second call queued behind the first for >= ~50ms.
        assert r2.timing["queue_wait_ms"] >= 30.0

    def test_gauge_reports_endpoint_pressure(self):
        limiter = EndpointLimiter(
            max_concurrent=4,
            overload_latency_threshold=120,
            provider="nv_inference",
            base_url="https://x",
        )
        limiter.waiting = 8
        g = limiter.gauge()
        assert g["provider"] == "nv_inference"
        assert g["waiting"] == 8
        assert g["max_concurrent"] == 4
        assert g["latency_ewma_ms"] == pytest.approx(3000.0)
        assert g["projected_call_ms"] == pytest.approx(9000.0)


class TestLimiterPool:
    def test_same_endpoint_shares_limiter(self):
        pool = LimiterPool()
        a = pool.get("nv_inference", "https://x", max_concurrent=4, overload_latency_threshold=120)
        b = pool.get("nv_inference", "https://x", max_concurrent=4, overload_latency_threshold=120)
        c = pool.get("nv_inference", "https://y", max_concurrent=4, overload_latency_threshold=120)
        assert a is b
        assert a is not c

    def test_conflicting_config_keeps_first(self, caplog):
        pool = LimiterPool()
        a = pool.get("nv_inference", "https://x", max_concurrent=4, overload_latency_threshold=120)
        b = pool.get("nv_inference", "https://x", max_concurrent=9, overload_latency_threshold=1)
        assert a is b
        assert b.max_concurrent == 4

    def test_create_backend_shares_limiter_across_backend_names(self, monkeypatch):
        import sys
        import types

        module = types.ModuleType("openai")
        module.AsyncOpenAI = lambda **kwargs: SimpleNamespace()
        monkeypatch.setitem(sys.modules, "openai", module)

        pool = LimiterPool()
        cfg_a = LLMBackendConfig(provider="nv_inference", model="m1", api_key="k")
        cfg_b = LLMBackendConfig(provider="nv_inference", model="m2", api_key="k")
        backend_a = create_backend(cfg_a, limiter_pool=pool)
        backend_b = create_backend(cfg_b, limiter_pool=pool)

        assert isinstance(backend_a, LimitedBackend)
        assert isinstance(backend_b, LimitedBackend)
        assert backend_a.limiter is backend_b.limiter

    def test_create_backend_without_pool_returns_bare_backend(self, monkeypatch):
        import sys
        import types

        module = types.ModuleType("openai")
        module.AsyncOpenAI = lambda **kwargs: SimpleNamespace()
        monkeypatch.setitem(sys.modules, "openai", module)

        cfg = LLMBackendConfig(provider="nv_inference", model="m1", api_key="k")
        backend = create_backend(cfg)
        assert not isinstance(backend, LimitedBackend)


class TestErrorNormalization:
    @pytest.mark.asyncio
    async def test_429_becomes_rate_limit_error_with_retry_after(self):
        provider_error = Exception("429 Too Many Requests")
        provider_error.status_code = 429
        provider_error.response = SimpleNamespace(status_code=429, headers={"retry-after": "3"})
        backend = LimitedBackend(_ErrorBackend([provider_error]), EndpointLimiter(4))

        with pytest.raises(LLMRateLimitError) as exc:
            await backend.complete(MESSAGES)
        assert exc.value.retry_after == 3.0

    @pytest.mark.asyncio
    async def test_timeout_named_exception_becomes_timeout_error(self):
        class APITimeoutError(Exception):
            pass

        backend = LimitedBackend(
            _ErrorBackend([APITimeoutError("request timed out")]),
            EndpointLimiter(4),
        )
        with pytest.raises(LLMTimeoutError):
            await backend.complete(MESSAGES)

    @pytest.mark.asyncio
    async def test_other_errors_pass_through(self):
        backend = LimitedBackend(_ErrorBackend([RuntimeError("boom")]), EndpointLimiter(4))
        with pytest.raises(RuntimeError):
            await backend.complete(MESSAGES)


class TestAgentRetryBehavior:
    @pytest.mark.asyncio
    async def test_rate_limit_retry_honors_capped_retry_after(self, test_intent, monkeypatch):
        sleeps: list[float] = []

        async def fake_sleep(delay):
            sleeps.append(delay)

        monkeypatch.setattr("nomos.llm.council_agent.asyncio.sleep", fake_sleep)
        backend = _ErrorBackend([LLMRateLimitError("slow down", retry_after=999.0)])
        agent = _member_agent(backend, max_retry_after_seconds=10.0)

        result = await agent.evaluate_intent(test_intent)

        assert result.decision == "approve"
        assert backend.calls == 2
        assert sleeps == [10.0]

    @pytest.mark.asyncio
    async def test_retry_budget_stops_retries(self, test_intent, monkeypatch):
        async def fail_sleep(delay):  # pragma: no cover - must not be reached
            raise AssertionError("should not sleep once the budget is exhausted")

        monkeypatch.setattr("nomos.llm.council_agent.asyncio.sleep", fail_sleep)
        backend = _ErrorBackend([LLMRateLimitError("slow down", retry_after=5.0)])
        # Budget smaller than the retry sleep → deny without retrying.
        agent = _member_agent(backend, retry_budget_seconds=1.0, max_retry_after_seconds=10.0)

        result = await agent.evaluate_intent(test_intent)

        assert result.decision == "deny"
        assert backend.calls == 1
        assert "failed after 1 attempts" in result.reasoning

    @pytest.mark.asyncio
    async def test_timeout_is_not_retried(self, test_intent):
        backend = _ErrorBackend([LLMTimeoutError("timed out")])
        agent = _member_agent(backend)

        result = await agent.evaluate_intent(test_intent)

        assert result.decision == "deny"
        assert backend.calls == 1
