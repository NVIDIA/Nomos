# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Engineer runtime regressions, including real lock-version SDK clients."""

import asyncio
import errno
import json
import logging
import ssl

import anthropic
import httpx
import openai
import pytest

from nomos.config.settings import CouncilMemberConfig
from nomos.level_0.types import Intent
from nomos.llm.anthropic_backend import AnthropicBackend
from nomos.llm.base import (
    LLMBackend,
    LLMCallCandidate,
    LLMRateLimitError,
    LLMResponse,
    LLMTimeoutError,
)
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


STUB_API_KEY = "<local-http-stub>"


def candidates(primary, backup, timeout=0.05):
    return tuple(
        LLMCallCandidate(name, backend.provider_name(), name, timeout, backend)
        for name, backend in (("primary", primary), ("backup", backup))
    )


def run_route(primary, backup, *, candidate_timeout=0.05, **kwargs):
    return _complete_with_retry(
        primary,
        [],
        temperature=0.3,
        max_tokens=237,
        parse=lambda text: _parse_evaluation(text, raise_on_parse_error=True),
        log_ctx="engineer",
        call_candidates=candidates(primary, backup, candidate_timeout),
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
    task = asyncio.create_task(
        run_route(primary, backup, candidate_timeout=2, retry_budget_seconds=3)
    )
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
        intent_id="intent-engineer",
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
@pytest.mark.parametrize(
    "status", [408, 429, 500, 502, 503, 504, 529, 400, 401, 403, 404, 422, 501, 505, 599]
)
@pytest.mark.asyncio
async def test_actual_sdk_statuses_have_finite_wire_count(provider, status):
    wire = []

    def handle(request):
        wire.append(request)
        return httpx.Response(status, json={"error": {"type": "api_error", "message": "stub"}})

    client_type = openai.AsyncOpenAI if provider == "openai" else anthropic.AsyncAnthropic
    backend_type = OpenAIBackend if provider == "openai" else AnthropicBackend
    backend = backend_type(api_key=STUB_API_KEY, max_retries=0)
    await backend._client.close()
    backend._client = client_type(
        api_key=STUB_API_KEY,
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    primary = LimitedBackend(backend, EndpointLimiter(1))
    backup = FakeBackend()
    try:
        if status in {408, 429, 500, 502, 503, 504} or (provider == "anthropic" and status == 529):
            _, response, _, _ = await run_route(primary, backup, candidate_timeout=1)
            assert response.execution.selected_backend == "backup"
        else:
            with pytest.raises(_RetryExhaustedError):
                await run_route(primary, backup, candidate_timeout=1)
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
    backend = backend_type(api_key=STUB_API_KEY, max_retries=0)
    await backend._client.close()
    backend._client = client_type(
        api_key=STUB_API_KEY,
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    backup = FakeBackend()
    try:
        if available:
            await run_route(backend, backup, candidate_timeout=1)
            assert len(backup.calls) == 1
        else:
            with pytest.raises(_RetryExhaustedError):
                await run_route(backend, backup, candidate_timeout=1)
            assert not backup.calls
        assert len(wire) == 1
    finally:
        await backend._client.close()


def test_class_names_do_not_establish_availability():
    class APITimeoutError(Exception):
        pass

    assert classify_availability_error(APITimeoutError()) is None


@pytest.mark.asyncio
async def test_legacy_normalization_cannot_grant_availability():
    class ProgrammingTimeoutError(Exception):
        pass

    class ArbitraryStatusError(Exception):
        status_code = 429

    for cause in (ProgrammingTimeoutError(), ArbitraryStatusError()):
        backup = FakeBackend()
        primary = LimitedBackend(FakeBackend(cause), EndpointLimiter(1))
        with pytest.raises(_RetryExhaustedError) as error:
            await run_route(primary, backup)
        assert error.value.execution.attempts[0].outcome == "terminal_error"
        assert not backup.calls


@pytest.mark.parametrize("stage", ["backup", "retry_after"])
@pytest.mark.asyncio
async def test_cancellation_in_later_stage_starts_no_further_work(stage):
    from nomos.llm.base import LLMRateLimitError

    primary = FakeBackend(
        LLMTimeoutError() if stage == "backup" else LLMRateLimitError(retry_after=0.5)
    )
    backup = FakeBackend(delay=5)
    task = asyncio.create_task(
        run_route(primary, backup, candidate_timeout=2, retry_budget_seconds=3)
    )
    await asyncio.sleep(0.01)
    task.cancel("cancel later stage")
    with pytest.raises(asyncio.CancelledError) as error:
        await task
    execution = error.value.llm_execution
    assert execution.selected_backend is None
    if stage == "backup":
        assert backup.finished and execution.attempts[-1].backend_id == "backup"
        assert execution.attempts[-1].outcome == "cancelled"
    else:
        assert not backup.calls and execution.attempts[-1].backend_id == "primary"


@pytest.mark.asyncio
async def test_interleaved_calls_on_same_agent_have_separate_attribution():
    class InterleavedBackend(FakeBackend):
        async def complete(self, messages, **kwargs):
            if "intent-backup" in messages[-1].content:
                await asyncio.sleep(0.01)
                raise LLMTimeoutError()
            return await super().complete(messages, **kwargs)

    primary, backup = InterleavedBackend(), FakeBackend(provider="anthropic")
    member = CouncilMemberAgent(
        CouncilMemberConfig(id="member", backend="primary"),
        primary,
        call_candidates=candidates(primary, backup, 1),
    )
    intents = [
        Intent(
            intent_id=f"intent-{route}",
            agent_id="engineer",
            action_type="file.read",
            description="Inspect status",
            target_resource="/workspace/status",
        )
        for route in ("primary", "backup")
    ]
    first, second = await asyncio.gather(*(member.evaluate_intent(intent) for intent in intents))
    assert first.execution.selected_backend == "primary"
    assert second.execution.selected_backend == "backup"
    assert first.execution.attempts[-1].provider == "openai"
    assert second.execution.attempts[-1].provider == "anthropic"
    assert len(first.execution.attempts) == 1 and len(second.execution.attempts) == 2


@pytest.mark.asyncio
async def test_code_review_fallback_ignores_forged_execution_and_keeps_coverage():
    from nomos.level_0.types.packaged_file import PackagedFile
    from nomos.llm.council_agent import CodeReviewAgent, format_code_review_analysis

    packaged = PackagedFile(path="status.py", content_base64="cHJpbnQoMSkK")
    content = {
        "decision": "approve",
        "reasoning": "Prints one value. No external effects.",
        "reviewed_all_content": True,
        "claim_match": "yes",
        "overall_risk": "low",
        "risk_concerns": [],
        "conditions": [],
        "guidance": "",
        "execution": {"attempts": "malicious malformed metadata"},
        "files": [
            {
                "path": packaged.path,
                "sha256": packaged.sha256,
                "mode": packaged.mode,
                "review_status": "complete",
                "matches_claimed_behavior": "yes",
                "observed_operations": ["prints one"],
                "risk_findings": [],
                "evidence": ["print(1)"],
            }
        ],
    }
    primary, backup = FakeBackend(LLMTimeoutError()), FakeBackend(json.dumps(content), "anthropic")
    agent = CodeReviewAgent(primary, call_candidates=candidates(primary, backup, 1))
    intent = Intent(
        intent_id="intent-code-review",
        agent_id="engineer",
        action_type="file.write",
        description="Print one value",
        target_resource="/workspace",
        packaged_files=[packaged],
    )
    result = await agent.review_intent_files(intent)
    assert result.decision == "approve" and result.execution.selected_backend == "backup"
    assert "execution" not in format_code_review_analysis(result)
    content["files"] = []
    backup.value = json.dumps(content)
    failed = await agent.review_intent_files(intent)
    assert failed.decision == "deny"
    assert failed.execution.selected_backend is None
    assert failed.execution.attempts[-1].outcome == "invalid_response"


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("mode", ["malformed", "cancel"])
@pytest.mark.asyncio
async def test_real_sdk_success_validation_and_cancellation(provider, mode):
    started = asyncio.Event()
    finished = asyncio.Event()
    wire = []

    async def handle(request):
        wire.append(request)
        started.set()
        if mode == "malformed":
            return httpx.Response(200, json=wire_success(provider, "invalid json"))
        try:
            await asyncio.sleep(10)
        finally:
            finished.set()

    backend_type = OpenAIBackend if provider == "openai" else AnthropicBackend
    client_type = openai.AsyncOpenAI if provider == "openai" else anthropic.AsyncAnthropic
    backend = backend_type(api_key=STUB_API_KEY, max_retries=0)
    await backend._client.close()
    backend._client = client_type(
        api_key=STUB_API_KEY,
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    backup = FakeBackend()
    primary = LimitedBackend(backend, EndpointLimiter(1))
    try:
        if mode == "malformed":
            with pytest.raises(_RetryExhaustedError) as error:
                await run_route(primary, backup, candidate_timeout=1)
            assert error.value.execution.attempts[-1].outcome == "invalid_response"
        else:
            task = asyncio.create_task(run_route(primary, backup, candidate_timeout=3))
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel("SDK cancellation")
            with pytest.raises(asyncio.CancelledError) as error:
                await task
            assert error.value.llm_execution.attempts[-1].outcome == "cancelled"
            assert finished.is_set()
        assert len(wire) == 1 and not backup.calls
        assert primary.limiter.waiting == 0
        async with primary.limiter.acquire():
            pass
    finally:
        await backend._client.close()


@pytest.mark.asyncio
async def test_pre_normalized_timeout_releases_primary_before_backup():
    limiter = EndpointLimiter(1)
    primary = LimitedBackend(FakeBackend(LLMTimeoutError()), limiter)

    class SameEndpointBackup(FakeBackend):
        async def complete(self, messages, **kwargs):
            async with limiter.acquire():
                return await super().complete(messages, **kwargs)

    backup = SameEndpointBackup(provider="anthropic")
    _, response, _, _ = await run_route(primary, backup, candidate_timeout=1)
    assert response.execution.selected_backend == "backup"
    assert limiter.waiting == 0
    assert [a.outcome for a in response.execution.attempts] == ["timeout", "success"]


@pytest.mark.asyncio
async def test_service_cancelled_debate_actor_uses_backup_execution():
    from nomos.nat.functions import CouncilService

    started = asyncio.Event()

    class WaitingBackup(FakeBackend):
        async def complete(self, messages, **kwargs):
            started.set()
            return await super().complete(messages, **kwargs)

    primary = LimitedBackend(FakeBackend(LLMTimeoutError()), EndpointLimiter(1))
    backup = LimitedBackend(WaitingBackup(provider="anthropic", delay=10), EndpointLimiter(1))
    member = CouncilMemberAgent(
        CouncilMemberConfig(id="member", backend="primary"),
        primary,
        call_candidates=candidates(primary, backup, 2),
    )
    service = CouncilService.__new__(CouncilService)
    service._agents = [member]
    intent = Intent(
        intent_id="intent-cancel-debate",
        agent_id="engineer",
        action_type="file.read",
        description="Inspect status file",
        target_resource="/workspace/status",
    )
    actors = []
    task = asyncio.create_task(service._run_debate_round(intent, [], 1, actor_timings=actors))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel("operator cancellation")
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(actors) == 1
    actor = actors[0]
    assert actor["role"] == "council_member_debate_1" and actor["status"] == "cancelled"
    assert actor["provider"] == "anthropic" and actor["backend"] == "backup"
    assert [attempt["outcome"] for attempt in actor["llm_attempts"]] == ["timeout", "cancelled"]
    assert primary.limiter.waiting == backup.limiter.waiting == 0


@pytest.mark.parametrize("backup_text", [APPROVE, DENY, "invalid json"])
@pytest.mark.asyncio
async def test_anthropic_overload_walks_approved_sdk_route_once(backup_text):
    wire = []

    def handle(request):
        wire.append(json.loads(request.content))
        if len(wire) == 1:
            return httpx.Response(
                529, json={"error": {"type": "overloaded_error", "message": "offline stub"}}
            )
        return httpx.Response(200, json=wire_success("anthropic", backup_text))

    backends = []
    for _ in range(2):
        backend = AnthropicBackend(api_key=STUB_API_KEY, max_retries=0)
        await backend._client.close()
        backend._client = anthropic.AsyncAnthropic(
            api_key=STUB_API_KEY,
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        )
        backends.append(backend)
    primary, backup = backends
    try:
        if backup_text == "invalid json":
            with pytest.raises(_RetryExhaustedError) as error:
                await run_route(primary, backup, candidate_timeout=2)
            execution = error.value.execution
            assert execution.selected_backend is None
            outcome = "invalid_response"
        else:
            result, response, _, _ = await run_route(primary, backup, candidate_timeout=2)
            assert result.decision == ("approve" if backup_text == APPROVE else "deny")
            execution = response.execution
            assert execution.selected_backend == "backup"
            assert execution.attempts[-1].configured_model == "backup"
            outcome = "success"
        assert [a.outcome for a in execution.attempts] == ["unavailable", outcome]
        assert len(wire) == 2 and wire[0] == wire[1]
        assert wire[0]["temperature"] == 0.3 and wire[0]["max_tokens"] == 237
    finally:
        for backend in backends:
            await backend._client.close()


@pytest.mark.parametrize("wrapper", [None, LLMTimeoutError, LLMRateLimitError])
@pytest.mark.parametrize("link", ["__cause__", "__context__"])
def test_anthropic_overload_chain_keeps_terminal_status_veto(wrapper, link):
    request = httpx.Request("POST", "https://offline.invalid")
    overloaded = anthropic.APIStatusError(
        "offline overload", response=httpx.Response(529, request=request), body=None
    )
    error = overloaded
    if wrapper is not None:
        error = wrapper("normalized")
        error.status_code = None
        setattr(error, link, overloaded)
    assert classify_availability_error(error) == "unavailable"
    terminal = anthropic.APIStatusError(
        "offline auth failure", response=httpx.Response(401, request=request), body=None
    )
    setattr(overloaded, link, terminal)
    assert classify_availability_error(error) is None


@pytest.mark.parametrize("wrapper", [None, LLMTimeoutError, LLMRateLimitError])
@pytest.mark.parametrize("source", ["openai", "arbitrary"])
def test_non_anthropic_529_stays_terminal_under_normalization(source, wrapper):
    if source == "openai":
        cause = openai.APIStatusError(
            "offline overload",
            response=httpx.Response(529, request=httpx.Request("POST", "https://offline.invalid")),
            body=None,
        )
    else:
        cause = RuntimeError("offline overload")
        cause.status_code = 529
    error = cause
    if wrapper is not None:
        error = wrapper("normalized")
        error.__cause__ = cause
    assert classify_availability_error(error) is None


class MarkerStatus(int):
    def __str__(self):
        return "PRIVATE_STATUS_MARKER"

    def __repr__(self):
        return "PRIVATE_STATUS_MARKER"


@pytest.mark.parametrize("status", [None, True, "529", 529.0, -1, 600, MarkerStatus(529)])
def test_malformed_anthropic_status_does_not_establish_overload(status):
    error = anthropic.APIStatusError(
        "offline stub",
        response=httpx.Response(529, request=httpx.Request("POST", "https://offline.invalid")),
        body=None,
    )
    error.status_code = status
    assert classify_availability_error(error) is None


@pytest.mark.parametrize(
    "status", [401, 100, 599, None, True, "401", -1, 600, MarkerStatus(401), object()]
)
@pytest.mark.asyncio
async def test_attempt_warning_contains_only_class_and_safe_status(caplog, status):
    error = RuntimeError("PRIVATE_MESSAGE_MARKER https://PRIVATE_ENDPOINT_MARKER.invalid")
    error.status_code = status
    primary, backup = FakeBackend(error), FakeBackend()
    with caplog.at_level(logging.INFO, logger="nomos.llm.council_agent"):
        with pytest.raises(_RetryExhaustedError) as raised:
            await run_route(primary, backup)
    records = [r for r in caplog.records if r.msg.startswith("llm_attempt ")]
    assert len(records) == 1 and records[0].levelno == logging.WARNING
    safe_status = status if type(status) is int and 100 <= status <= 599 else None
    message = records[0].getMessage()
    assert "error_class=RuntimeError" in message and f"status_code={safe_status}" in message
    assert "PRIVATE_" not in message and records[0].exc_info is None
    assert "PRIVATE_" not in raised.value.execution.model_dump_json()
    assert not backup.calls


@pytest.mark.asyncio
async def test_invalid_response_warning_and_success_info_remain_sanitized(caplog):
    with caplog.at_level(logging.INFO, logger="nomos.llm.council_agent"):
        with pytest.raises(_RetryExhaustedError) as raised:
            await run_route(FakeBackend("PRIVATE_BODY_MARKER"), FakeBackend())
        await run_route(FakeBackend(), FakeBackend())
    records = [r for r in caplog.records if r.msg.startswith("llm_attempt ")]
    assert len(records) == 2
    failed, success = records
    assert failed.levelno == logging.WARNING and failed.exc_info is None
    assert "error_class=" in failed.getMessage() and "status_code=None" in failed.getMessage()
    assert "PRIVATE_" not in failed.getMessage()
    assert "PRIVATE_" not in raised.value.execution.model_dump_json()
    assert success.levelno == logging.INFO and "outcome=success" in success.getMessage()


@pytest.mark.parametrize("wrapper", [None, LLMTimeoutError, LLMRateLimitError])
@pytest.mark.asyncio
async def test_sdk_auth_warning_excludes_response_and_wrapper_messages(caplog, wrapper):
    request = httpx.Request("POST", "https://PRIVATE_ENDPOINT_MARKER.invalid")
    cause = anthropic.APIStatusError(
        "PRIVATE_MESSAGE_MARKER",
        response=httpx.Response(
            401,
            request=request,
            headers={"x-offline-stub": "PRIVATE_HEADER_MARKER"},
            text="PRIVATE_BODY_MARKER",
        ),
        body=None,
    )
    error = cause
    if wrapper is not None:
        error = wrapper("PRIVATE_WRAPPER_MARKER")
        error.status_code = None
        error.__cause__ = cause
    with caplog.at_level(logging.INFO, logger="nomos.llm.council_agent"):
        with pytest.raises(_RetryExhaustedError) as raised:
            await run_route(FakeBackend(error), FakeBackend())
    records = [r for r in caplog.records if r.msg.startswith("llm_attempt ")]
    assert len(records) == 1 and records[0].levelno == logging.WARNING
    message = records[0].getMessage()
    assert f"error_class={type(error).__name__}" in message
    assert f"status_code={401 if wrapper is None else None}" in message
    assert "PRIVATE_" not in message and records[0].exc_info is None
    assert "PRIVATE_" not in raised.value.execution.model_dump_json()


@pytest.mark.parametrize("same_response", [False, True])
def test_http_status_context_requires_genuine_anthropic_overload_response(same_response):
    request = httpx.Request("POST", "https://offline.invalid")
    response = httpx.Response(529, request=request)
    context_response = response if same_response else httpx.Response(529, request=request)
    error = anthropic.APIStatusError("offline overload", response=response, body=None)
    error.__context__ = httpx.HTTPStatusError(
        "offline transport status", request=request, response=context_response
    )
    assert classify_availability_error(error) == ("unavailable" if same_response else None)


def test_terminal_context_vetoes_anthropic_overload_with_explicit_cause():
    request = httpx.Request("POST", "https://offline.invalid")
    error = anthropic.APIStatusError(
        "offline overload", response=httpx.Response(529, request=request), body=None
    )
    error.__cause__ = anthropic.APIStatusError(
        "offline transient", response=httpx.Response(503, request=request), body=None
    )
    error.__context__ = anthropic.APIStatusError(
        "offline auth failure", response=httpx.Response(401, request=request), body=None
    )
    assert classify_availability_error(error) is None
