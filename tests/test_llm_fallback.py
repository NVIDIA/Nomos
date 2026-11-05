# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Engineer runtime regressions, including real lock-version SDK clients."""

import asyncio
import errno
import json
import ssl

import anthropic
import httpx
import openai
import pytest

from nomos.config.settings import CouncilMemberConfig
from nomos.level_0.types import Intent
from nomos.llm.anthropic_backend import AnthropicBackend
from nomos.llm.base import LLMBackend, LLMCallCandidate, LLMResponse, LLMTimeoutError
from nomos.llm.council_agent import (
    ChairmanAgent,
    CouncilMemberAgent,
    _complete_with_retry,
    _parse_evaluation,
    _RetryExhaustedError,
)
from nomos.llm.limiter import EndpointLimiter, LimitedBackend, classify_availability_error
from nomos.llm.openai_backend import OpenAIBackend

APPROVE = json.dumps({"decision": "approve", "reasoning": "Specific review passed."})
DENY = json.dumps({"decision": "deny", "reasoning": "Specific review denied."})


class FakeBackend(LLMBackend):
    def __init__(self, value=APPROVE, provider="openai", delay=0):
        self.value, self.provider, self.delay = value, provider, delay
        self.calls = []
        self.finished = False

    async def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        try:
            await asyncio.sleep(self.delay)
            if isinstance(self.value, Exception):
                raise self.value
            return LLMResponse(content=self.value, model=f"returned-{self.provider}")
        finally:
            self.finished = True

    def provider_name(self):
        return self.provider


def candidates(primary, backup, timeout=0.05):
    return tuple(
        LLMCallCandidate(name, backend.provider_name(), name, timeout, backend)
        for name, backend in (("primary", primary), ("backup", backup))
    )


def run_route(primary, backup, **kwargs):
    return _complete_with_retry(
        primary,
        [],
        temperature=0.3,
        max_tokens=237,
        parse=lambda text: _parse_evaluation(text, raise_on_parse_error=True),
        log_ctx="engineer",
        call_candidates=candidates(primary, backup),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_primary_timeout_uses_backup_once_and_preserves_options():
    primary, backup = FakeBackend(delay=1), FakeBackend(provider="anthropic")
    result, response, index, _ = await run_route(primary, backup, retry_budget_seconds=0.2)
    assert result.decision == "approve" and index == 1
    assert primary.finished and len(primary.calls) == len(backup.calls) == 1
    assert primary.calls == backup.calls
    assert response.execution.selected_backend == "backup"
    assert [a.outcome for a in response.execution.attempts] == ["timeout", "success"]
    assert response.execution.attempts[-1].provider == "anthropic"


@pytest.mark.parametrize(
    "value",
    [
        DENY,
        "bad json",
        "",
        '{"decision":"approve"}',
        ValueError("invalid configuration"),
        RuntimeError("unknown"),
    ],
)
@pytest.mark.asyncio
async def test_terminal_response_or_error_never_selects_backup(value):
    primary, backup = FakeBackend(value), FakeBackend()
    if value == DENY:
        result, response, _, _ = await run_route(primary, backup)
        assert result.decision == "deny" and response.execution.selected_backend == "primary"
    else:
        with pytest.raises(_RetryExhaustedError) as error:
            await run_route(primary, backup)
        assert error.value.attempts == 1
        assert error.value.execution.selected_backend is None
    assert not backup.calls


@pytest.mark.asyncio
async def test_exhausted_route_never_invents_usage_or_revisits_primary():
    primary, backup = FakeBackend(LLMTimeoutError()), FakeBackend(LLMTimeoutError())
    with pytest.raises(_RetryExhaustedError) as error:
        await run_route(primary, backup)
    assert len(primary.calls) == len(backup.calls) == 1
    assert error.value.timing() == {}
    assert len(error.value.execution.attempts) == 2


@pytest.mark.asyncio
async def test_single_deadline_stops_route_and_cleans_up():
    primary, backup = FakeBackend(delay=1), FakeBackend(delay=1)
    start = asyncio.get_running_loop().time()
    with pytest.raises(_RetryExhaustedError):
        await run_route(primary, backup, retry_budget_seconds=0.07)
    assert asyncio.get_running_loop().time() - start < 0.3
    assert primary.finished and backup.finished


@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.asyncio
async def test_cancellation_propagates_and_releases_endpoint(queued):
    limiter = EndpointLimiter(1)
    inner, backup = FakeBackend(delay=5), FakeBackend()
    primary = LimitedBackend(inner, limiter)
    if queued:
        await limiter._semaphore.acquire()
    task = asyncio.create_task(run_route(primary, backup, retry_budget_seconds=3))
    await asyncio.sleep(0.01)
    task.cancel("operator cancellation")
    with pytest.raises(asyncio.CancelledError) as error:
        await task
    assert error.value.args == ("operator cancellation",)
    assert error.value.llm_execution.attempts[-1].outcome == "cancelled"
    assert not backup.calls and limiter.waiting == 0
    if queued:
        limiter._semaphore.release()
    async with limiter.acquire():
        pass


@pytest.mark.asyncio
async def test_member_debate_and_chairman_keep_trusted_metadata():
    primary, backup = FakeBackend(LLMTimeoutError()), FakeBackend(provider="anthropic")
    member = CouncilMemberAgent(
        CouncilMemberConfig(id="member", backend="primary"),
        primary,
        call_candidates=candidates(primary, backup),
    )
    intent = Intent(
        agent_id="agent",
        action_type="file.read",
        description="Read status",
        target_resource="/workspace/status",
        justification="Inspect status",
    )
    for result in (
        await member.evaluate_intent(intent),
        await member.debate_evaluate(intent, "debate"),
    ):
        assert result.execution.selected_backend == "backup"
        assert "execution" not in result.model_dump()
    backup.value = '{"threshold":"quorum","reasoning":"Low risk","risk_level":"low"}'
    chairman = ChairmanAgent(primary, call_candidates=candidates(primary, backup))
    decision = await chairman.pre_authorize(intent, {"read"}, {"read"})
    assert decision.threshold == "unanimous"
    assert decision.execution.selected_backend == "backup"
    count = len(primary.calls)
    assert not (await chairman.pre_authorize(intent, set(), {"read"})).authorized
    assert len(primary.calls) == count


def wire_success(provider, text=APPROVE):
    if provider == "openai":
        return {
            "id": "chat-engineer",
            "object": "chat.completion",
            "created": 1,
            "model": "wire-openai",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": text},
                }
            ],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        }
    return {
        "id": "msg-engineer",
        "type": "message",
        "role": "assistant",
        "model": "wire-anthropic",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 7, "output_tokens": 3},
    }


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504, 400, 401, 403, 404, 422, 501])
@pytest.mark.asyncio
async def test_actual_sdk_statuses_have_finite_wire_count(provider, status):
    wire = []

    def handle(request):
        wire.append(request)
        return httpx.Response(status, json={"error": {"type": "api_error", "message": "stub"}})

    client_type = openai.AsyncOpenAI if provider == "openai" else anthropic.AsyncAnthropic
    backend_type = OpenAIBackend if provider == "openai" else AnthropicBackend
    backend = backend_type(api_key="engineer-stub", max_retries=0)
    await backend._client.close()
    backend._client = client_type(
        api_key="engineer-stub",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    primary = LimitedBackend(backend, EndpointLimiter(1))
    backup = FakeBackend()
    try:
        if status in {408, 429, 500, 502, 503, 504}:
            _, response, _, _ = await run_route(primary, backup)
            assert response.execution.selected_backend == "backup"
        else:
            with pytest.raises(_RetryExhaustedError):
                await run_route(primary, backup)
            assert not backup.calls
        assert len(wire) == 1
    finally:
        await backend._client.close()


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize(
    "cause,available",
    [
        (ConnectionRefusedError(errno.ECONNREFUSED, "local stub"), True),
        (ssl.SSLCertVerificationError("certificate stub"), False),
        (ValueError("invalid configuration"), False),
        (RuntimeError("unknown connection failure"), False),
    ],
)
@pytest.mark.asyncio
async def test_actual_sdk_connection_cause_allowlist(provider, cause, available):
    wire = []

    def handle(request):
        wire.append(request)
        raise httpx.ConnectError("transport stub", request=request) from cause

    backend_type = OpenAIBackend if provider == "openai" else AnthropicBackend
    client_type = openai.AsyncOpenAI if provider == "openai" else anthropic.AsyncAnthropic
    backend = backend_type(api_key="engineer-stub", max_retries=0)
    await backend._client.close()
    backend._client = client_type(
        api_key="engineer-stub",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    backup = FakeBackend()
    try:
        if available:
            await run_route(backend, backup)
            assert len(backup.calls) == 1
        else:
            with pytest.raises(_RetryExhaustedError):
                await run_route(backend, backup)
            assert not backup.calls
        assert len(wire) == 1
    finally:
        await backend._client.close()


def test_class_names_do_not_establish_availability():
    class APITimeoutError(Exception):
        pass

    assert classify_availability_error(APITimeoutError()) is None
