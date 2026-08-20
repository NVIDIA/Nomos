# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from nomos.llm.council_agent import _parse_chairman_decision, _parse_evaluation


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

        assert _describe_failure(LLMTimeoutError("Request timed out.")) == (
            "provider timeout: Request timed out."
        )

    def test_rate_limit_is_distinguished_from_a_timeout(self):
        from nomos.llm.base import LLMRateLimitError
        from nomos.llm.council_agent import _describe_failure

        described = _describe_failure(LLMRateLimitError("Too Many Requests"))
        assert described == "provider rate limit: Too Many Requests"
        assert "timeout" not in described

    def test_http_status_is_surfaced_when_the_provider_supplies_one(self):
        from nomos.llm.council_agent import _describe_failure

        class _ProviderError(Exception):
            status_code = 400

        described = _describe_failure(_ProviderError("DEGRADED function cannot be invoked"))
        assert "HTTP 400" in described
        assert "DEGRADED function cannot be invoked" in described

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

        assert _describe_failure(ValueError()) == "ValueError: ValueError"
