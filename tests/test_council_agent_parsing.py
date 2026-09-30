# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import base64

import pytest

from nomos.config.settings import CouncilMemberConfig
from nomos.level_0.types import Intent
from nomos.level_0.types.packaged_file import PackagedFile
from nomos.llm.base import LLMRateLimitError
from nomos.llm.council_agent import (
    ChairmanAgent,
    CodeReviewAgent,
    CouncilMemberAgent,
    _parse_chairman_decision,
    _parse_evaluation,
)


def test_parse_evaluation_accepts_trailing_text_after_json():
    response = """
    {
      "decision": "approve",
      "reasoning": "The command is read-only and tightly scoped. The target matches the request.",
      "risk_concerns": [],
      "conditions": [],
      "guidance": ""
    }
    Extra commentary the model should not have returned.
    """

    parsed = _parse_evaluation(response)

    assert parsed.decision == "approve"
    assert "read-only" in parsed.reasoning


def test_parse_chairman_decision_accepts_fenced_json_with_trailing_text():
    response = """```json
    {
      "threshold": "quorum",
      "reasoning": "This is a bounded planning step with no live infrastructure changes.",
      "risk_level": "low"
    }
    ```
    Additional notes.
    """

    parsed = _parse_chairman_decision(response)

    assert parsed.threshold == "quorum"
    assert parsed.risk_level == "low"


class TestFailureDescription:
    """A member's failure reasoning must let an operator pick the right remedy.

    The three cases below have different fixes -- wait, raise the budget, or change
    the model -- and previously all three rendered as bare `str(error)`, so a
    provider refusing requests and a provider being slow produced sentences an
    operator could not tell apart.
    """

    def test_timeout_is_named_as_a_timeout(self):
        from nomos.llm.base import LLMTimeoutError
        from nomos.llm.council_agent import _describe_failure

        assert _describe_failure(LLMTimeoutError("Request timed out.")) == "provider timeout"

    def test_rate_limit_is_distinguished_from_a_timeout(self):
        from nomos.llm.base import LLMRateLimitError
        from nomos.llm.council_agent import _describe_failure

        described = _describe_failure(LLMRateLimitError("Too Many Requests"))
        assert described == "provider rate limit"
        assert "timeout" not in described

    def test_http_status_is_surfaced_when_the_provider_supplies_one(self):
        from nomos.llm.council_agent import _describe_failure

        class _ProviderError(Exception):
            status_code = 400

        described = _describe_failure(_ProviderError("DEGRADED function cannot be invoked"))
        assert "HTTP 400" in described
        assert "DEGRADED function cannot be invoked" not in described
        assert described == "_ProviderError (HTTP 400)"

    def test_status_is_read_from_a_nested_response_object(self):
        """openai-style SDK errors carry the status on `.response`, not the exception."""
        from nomos.llm.council_agent import _describe_failure

        class _Response:
            status_code = 429

        class _SdkError(Exception):
            response = _Response()

        assert "HTTP 429" in _describe_failure(_SdkError("rate limited"))

    def test_an_error_with_no_message_still_names_its_type(self):
        from nomos.llm.council_agent import _describe_failure

        assert _describe_failure(ValueError()) == "ValueError"


class TestCallerFailurePaths:
    """Every caller must fail closed AND say why, naming the backend.

    These exist because the diagnostic content was previously asserted only for
    `_describe_failure` in isolation. Two of the four callers had quietly dropped
    the provider from their fallback message, and nothing caught it: the unit
    test for the description helper still passed, because the helper was never
    the thing that was broken.
    """

    class _DeadBackend:
        """A backend that always fails, with a status the message must surface."""

        def __init__(self, exc: Exception, provider: str = "nim-inkling"):
            self._exc = exc
            self._provider = provider

        def provider_name(self) -> str:
            return self._provider

        def model_name(self) -> str:
            return "test-model"

        async def complete(self, *args, **kwargs):
            raise self._exc

    @staticmethod
    def _rate_limited() -> Exception:
        err = LLMRateLimitError("Too Many Requests")
        err.status_code = 429
        return err

    @staticmethod
    def _packaged_intent() -> Intent:
        """An intent WITH files, so Code Review actually calls its backend.

        Without packaged files `review_intent_files` short-circuits to approve --
        correctly, since there is nothing to review -- and never reaches the
        failure path this test is about.
        """
        intent = TestCallerFailurePaths._intent()
        intent.packaged_files = [
            PackagedFile(
                path="deploy/run.sh",
                content_base64=base64.b64encode(b"#!/bin/sh\necho hello\n").decode(),
            ),
            PackagedFile(
                path="deploy/config.yaml",
                content_base64=base64.b64encode(b"replicas: 1\n").decode(),
            ),
        ]
        return intent

    @staticmethod
    def _intent() -> Intent:
        return Intent(
            intent_id="intent-failure-path",
            agent_id="agent-under-test",
            command="/bin/echo hello",
            working_directory="/workspace",
            description="A benign command used to drive the failure path",
            target_resource="/workspace",
            justification="Exercising caller failure handling",
        )

    def _assert_diagnostic(self, text: str) -> None:
        """The three facts an operator needs to pick a remedy."""
        assert "provider rate limit" in text, f"failure kind missing from: {text!r}"
        assert "HTTP 429" in text, f"HTTP status missing from: {text!r}"
        assert "nim-inkling" in text, f"provider/backend missing from: {text!r}"

    @pytest.mark.asyncio
    async def test_evaluate_intent_denies_and_names_the_backend(self):
        agent = CouncilMemberAgent(
            CouncilMemberConfig(id="environment-isolation", backend="nim-inkling"),
            self._DeadBackend(self._rate_limited()),
            max_retries=0,
        )
        result = await agent.evaluate_intent(self._intent())
        assert result.decision == "deny", "a member that cannot evaluate must fail closed"
        self._assert_diagnostic(result.reasoning)

    @pytest.mark.asyncio
    async def test_debate_evaluate_denies_and_names_the_backend(self):
        agent = CouncilMemberAgent(
            CouncilMemberConfig(id="environment-isolation", backend="nim-inkling"),
            self._DeadBackend(self._rate_limited()),
            max_retries=0,
        )
        result = await agent.debate_evaluate(self._intent(), [])
        assert result.decision == "deny"
        self._assert_diagnostic(result.reasoning)

    @pytest.mark.asyncio
    async def test_review_intent_files_denies_and_names_the_backend(self):
        intent = self._packaged_intent()
        agent = CodeReviewAgent(self._DeadBackend(self._rate_limited()), max_retries=0)
        result = await agent.review_intent_files(intent)
        assert result.decision == "deny"
        self._assert_diagnostic(result.reasoning)
        # Schema-complete: voting members require one assessment per packaged
        # file, so a failure must still describe every file rather than none.
        assert len(result.files) == len(intent.packaged_files)
        assert {f.path for f in result.files} == {f.path for f in intent.packaged_files}
        assert all(f.review_status == "inconclusive" for f in result.files)

    @pytest.mark.asyncio
    async def test_decide_threshold_falls_back_to_unanimous_and_names_the_backend(self):
        agent = ChairmanAgent(self._DeadBackend(self._rate_limited()), max_retries=0)
        result = await agent.decide_threshold(self._intent())
        # The chairman's safe fallback is the STRICTER threshold, not a denial:
        # it decides how many members must agree, and unanimity is fail-closed.
        assert result.threshold == "unanimous"
        self._assert_diagnostic(result.reasoning)
