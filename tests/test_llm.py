"""
Tests for LLM backend abstraction and council member agent.
"""

import json

import pytest

from nomos.config.settings import (
    CouncilMemberConfig,
    LLMBackendConfig,
)
from nomos.level_0.types import Intent, PackagedFile
from nomos.llm.base import LLMBackend, LLMMessage, LLMResponse, LLMRole
from nomos.llm.council_agent import (
    ChairmanAgent,
    CouncilMemberAgent,
    _format_intent_for_review,
    _parse_code_review_evaluation,
    _parse_evaluation,
    format_code_review_result_json,
)

# ── Mock LLM Backend ───────────────────────────────────────────────────


class MockLLMBackend(LLMBackend):
    """Mock LLM backend for testing."""

    def __init__(self, response_content: str = ""):
        self._response_content = response_content
        self.last_messages = []

    async def complete(self, messages, temperature=0.0, max_tokens=1024, response_format=None):
        self.last_messages = messages
        return LLMResponse(
            content=self._response_content,
            model="mock-model",
            usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        )

    def provider_name(self):
        return "mock"


class SequencedLLMBackend(LLMBackend):
    """Mock LLM backend that returns one response per call."""

    def __init__(self, responses: list[str]):
        self._responses = responses
        self.calls = 0
        self.call_kwargs = []
        self.call_messages = []

    async def complete(self, messages, temperature=0.0, max_tokens=1024, response_format=None):
        self.call_messages.append(messages)
        self.call_kwargs.append(
            {
                "temperature": temperature,
                "max_tokens": max_tokens,
                "response_format": response_format,
            }
        )
        response = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return LLMResponse(content=response, model="mock-model")

    def provider_name(self):
        return "mock"


# ── Tests ──────────────────────────────────────────────────────────────


class TestLLMMessage:
    """Tests for LLM message types."""

    def test_message_creation(self):
        msg = LLMMessage(role=LLMRole.SYSTEM, content="You are helpful")
        assert msg.role == LLMRole.SYSTEM
        assert msg.content == "You are helpful"

    def test_response_creation(self):
        resp = LLMResponse(content="Hello", model="gpt-4o")
        assert resp.content == "Hello"
        assert resp.usage == {}


class TestParseEvaluation:
    """Tests for evaluation response parsing."""

    def test_parse_valid_approve(self):
        content = json.dumps(
            {
                "decision": "approve",
                "reasoning": "Looks safe",
                "risk_concerns": [],
                "conditions": ["Monitor after execution"],
            }
        )
        result = _parse_evaluation(content)
        assert result.decision == "approve"
        assert result.reasoning == "Looks safe"
        assert result.conditions == ["Monitor after execution"]

    def test_parse_valid_deny(self):
        content = json.dumps(
            {
                "decision": "deny",
                "reasoning": "Too risky",
                "risk_concerns": ["Privilege escalation"],
            }
        )
        result = _parse_evaluation(content)
        assert result.decision == "deny"
        assert result.risk_concerns == ["Privilege escalation"]

    def test_parse_invalid_decision_defaults_deny(self):
        content = json.dumps({"decision": "maybe", "reasoning": "Unsure"})
        result = _parse_evaluation(content)
        assert result.decision == "deny"

    def test_parse_invalid_json_defaults_deny(self):
        result = _parse_evaluation("not json at all")
        assert result.decision == "deny"
        assert "Unparseable" in result.risk_concerns[0]

    def test_parse_empty_content(self):
        result = _parse_evaluation("")
        assert result.decision == "deny"

    def test_parse_markdown_fenced_json(self):
        content = '```json\n{"decision": "approve", "reasoning": "Looks safe"}\n```'
        result = _parse_evaluation(content)
        assert result.decision == "approve"
        assert result.reasoning == "Looks safe"

    def test_parse_markdown_fenced_no_lang(self):
        content = '```\n{"decision": "deny", "reasoning": "Too risky"}\n```'
        result = _parse_evaluation(content)
        assert result.decision == "deny"
        assert result.reasoning == "Too risky"

    def test_parse_code_review_requires_clear_json_schema(self):
        packaged_file = PackagedFile(
            path="scripts/task.py",
            content_base64="cHJpbnQoJ29rJykK",
            mode="0600",
            description="Small reviewed script",
        )
        intent = Intent.create(
            agent_id="agent-1",
            description="Run reviewed packaged script",
            target_resource="/workspace/out.txt",
            command="/usr/local/bin/python3 scripts/task.py",
            working_directory="/workspace",
            packaged_files=[packaged_file],
        )
        content = json.dumps(
            {
                "decision": "approve",
                "reviewed_all_content": True,
                "claim_match": "yes",
                "overall_risk": "low",
                "reasoning": "The script only prints a small marker. It matches the claim.",
                "files": [
                    {
                        "path": "scripts/task.py",
                        "sha256": packaged_file.sha256,
                        "mode": "0600",
                        "review_status": "complete",
                        "matches_claimed_behavior": "yes",
                        "observed_operations": ["prints ok to stdout"],
                        "risk_findings": [],
                        "evidence": ["print('ok')"],
                    }
                ],
                "risk_concerns": [],
                "conditions": [],
                "guidance": "",
            }
        )

        result = _parse_code_review_evaluation(content, intent)
        formatted = format_code_review_result_json(result)

        assert result.decision == "approve"
        assert result.files[0].path == "scripts/task.py"
        assert result.files[0].sha256 == packaged_file.sha256
        assert '"observed_operations": [' in formatted

    def test_parse_code_review_rejects_generic_vote_json(self):
        packaged_file = PackagedFile(
            path="scripts/task.py",
            content_base64="cHJpbnQoJ29rJykK",
            mode="0600",
        )
        intent = Intent.create(
            agent_id="agent-1",
            description="Run reviewed packaged script",
            target_resource="/workspace/out.txt",
            command="/usr/local/bin/python3 scripts/task.py",
            working_directory="/workspace",
            packaged_files=[packaged_file],
        )

        with pytest.raises(ValueError):
            _parse_code_review_evaluation(
                json.dumps({"decision": "approve", "reasoning": "Looks fine"}),
                intent,
            )


class TestFormatIntentForReview:
    """Tests for intent formatting."""

    def test_basic_formatting(self):
        intent = Intent.create(
            agent_id="agent-1",
            action_type="file.delete",
            description="Delete old log files from the server",
            target_resource="/var/log/app",
            command="find /var/log -name '*.log' -mtime +30 -delete",
            justification="Disk cleanup needed",
            blast_radius="Log files only",
        )
        formatted = _format_intent_for_review(intent)
        assert "file.delete" in formatted
        assert "Delete old log files" in formatted
        assert "find /var/log" in formatted
        assert "Disk cleanup" in formatted
        assert "Log files only" in formatted

    def test_kubernetes_fields(self):
        intent = Intent.create(
            agent_id="agent-1",
            action_type="deploy.application",
            description="Deploy application to Kubernetes cluster",
            target_resource="/apps/myapp",
            target_cluster="prod-us-east-1",
            target_namespace="default",
            target_container="app-container",
        )
        formatted = _format_intent_for_review(intent)
        assert "prod-us-east-1" in formatted
        assert "default" in formatted
        assert "app-container" in formatted

    def test_packaged_files_explain_code_review_responsibility(self):
        intent = Intent.create(
            agent_id="agent-1",
            description="Run reviewed packaged script",
            target_resource="/workspace/out.txt",
            command="/usr/local/bin/python3 scripts/task.py",
            working_directory="/workspace",
            packaged_files=[
                PackagedFile(
                    path="scripts/task.py",
                    content_base64="cHJpbnQoJ29rJykK",
                    mode="0600",
                    description="Small reviewed script",
                )
            ],
        )

        formatted = _format_intent_for_review(intent)

        assert "## Packaged Files" in formatted
        assert "Code Review council member receives the decoded file contents" in formatted
        assert "must" in formatted
        assert "not require the requester to paste the full file contents inline" in formatted
        assert "sha256=" in formatted

    def test_evidence_formatting_labels_certified_vs_agent_asserted(self):
        intent = Intent.create(
            agent_id="agent-1",
            description="Use operational evidence to decide a checkout service change.",
            target_resource="production/deployment/checkout-service",
            command="/usr/bin/kubectl -n production set env deployment/checkout-service X=1",
            working_directory="/workspace",
            evidence=[
                "plain text claim from the requesting agent",
                "CERTIFIED EXECUTOR EVIDENCE (signature verified, executor_key=abc123):\n"
                "stdout:\nverified output",
            ],
        )

        formatted = _format_intent_for_review(intent)

        assert "Evidence trust levels are explicit" in formatted
        assert "Evidence 1: AGENT-ASSERTED EVIDENCE" in formatted
        assert "Evidence 2: CERTIFIED EXECUTOR EVIDENCE" in formatted
        assert "not cryptographic proof" in formatted


class TestCouncilMemberAgent:
    """Tests for the LLM-backed council member agent."""

    @pytest.fixture
    def approve_agent(self):
        backend = MockLLMBackend(
            response_content=json.dumps(
                {
                    "decision": "approve",
                    "reasoning": "Action is safe and justified",
                    "risk_concerns": [],
                    "conditions": [],
                }
            )
        )
        config = CouncilMemberConfig(
            id="test-approver",
            backend="mock",
            weight=1,
            display_name="Test Approver",
            priorities=["Allow safe operations"],
        )
        return CouncilMemberAgent(member_config=config, backend=backend)

    @pytest.fixture
    def deny_agent(self):
        backend = MockLLMBackend(
            response_content=json.dumps(
                {
                    "decision": "deny",
                    "reasoning": "Too risky for production",
                    "risk_concerns": ["Service disruption"],
                    "conditions": [],
                }
            )
        )
        config = CouncilMemberConfig(
            id="test-denier",
            backend="mock",
            weight=2,
            display_name="Test Denier",
            priorities=["Deny risky operations"],
        )
        return CouncilMemberAgent(member_config=config, backend=backend)

    @pytest.fixture
    def test_intent(self):
        return Intent.create(
            agent_id="agent-1",
            action_type="file.delete",
            description="Delete old log files from the server",
            target_resource="/var/log/app",
            justification="Disk cleanup",
        )

    @pytest.mark.asyncio
    async def test_approve_evaluation(self, approve_agent, test_intent):
        result = await approve_agent.evaluate_intent(test_intent)
        assert result.decision == "approve"
        assert result.reasoning == "Action is safe and justified"

    @pytest.mark.asyncio
    async def test_deny_evaluation(self, deny_agent, test_intent):
        result = await deny_agent.evaluate_intent(test_intent)
        assert result.decision == "deny"
        assert "Too risky" in result.reasoning

    def test_agent_properties(self, approve_agent):
        assert approve_agent.member_id == "test-approver"
        assert approve_agent.weight == 1
        assert approve_agent.display_name == "Test Approver"

    @pytest.mark.asyncio
    async def test_system_prompt_sent(self, approve_agent, test_intent):
        await approve_agent.evaluate_intent(test_intent)
        backend = approve_agent._backend
        system_msg = backend.last_messages[0]
        assert system_msg.role == LLMRole.SYSTEM
        assert "council member" in system_msg.content.lower()
        assert "Allow safe operations" in system_msg.content

    @pytest.mark.asyncio
    async def test_error_handling_defaults_deny(self, test_intent):
        class ErrorBackend(LLMBackend):
            async def complete(self, messages, **kwargs):
                raise RuntimeError("API error")

            def provider_name(self):
                return "error"

        config = CouncilMemberConfig(
            id="error-agent",
            backend="mock",
            priorities=["Test error handling"],
        )
        agent = CouncilMemberAgent(member_config=config, backend=ErrorBackend())
        result = await agent.evaluate_intent(test_intent)
        assert result.decision == "deny"
        assert "failed" in result.reasoning.lower() or "error" in result.reasoning.lower()

    @pytest.mark.asyncio
    async def test_malformed_json_response_is_retried(self, test_intent, monkeypatch):
        monkeypatch.setattr("nomos.llm.council_agent.RETRY_DELAY_SECONDS", 0)
        backend = SequencedLLMBackend(
            [
                '{"decision": "approve", "reasoning": "truncated',
                json.dumps(
                    {
                        "decision": "approve",
                        "reasoning": "Recovered after malformed response",
                        "risk_concerns": [],
                        "conditions": [],
                    }
                ),
            ]
        )
        config = CouncilMemberConfig(
            id="retry-parse-agent",
            backend="mock",
            priorities=["Test parse retry"],
        )
        agent = CouncilMemberAgent(member_config=config, backend=backend)

        result = await agent.evaluate_intent(test_intent)

        assert result.decision == "approve"
        assert result.reasoning == "Recovered after malformed response"
        assert backend.calls == 2
        assert len(backend.call_messages[1]) > len(backend.call_messages[0])
        assert "previous response was not accepted" in backend.call_messages[1][-1].content

    @pytest.mark.asyncio
    async def test_agent_uses_configured_generation_budget(self, test_intent):
        backend = SequencedLLMBackend(
            [
                json.dumps(
                    {
                        "decision": "approve",
                        "reasoning": "Configured token budget reached the backend.",
                    }
                )
            ]
        )
        config = CouncilMemberConfig(
            id="configured-agent",
            backend="mock",
            priorities=["Test generation options"],
        )
        agent = CouncilMemberAgent(
            member_config=config,
            backend=backend,
            temperature=0.2,
            max_tokens=2048,
        )

        await agent.evaluate_intent(test_intent)

        assert backend.call_kwargs[0]["temperature"] == 0.2
        assert backend.call_kwargs[0]["max_tokens"] == 2048

    @pytest.mark.asyncio
    async def test_chairman_malformed_json_response_is_retried(self, test_intent, monkeypatch):
        monkeypatch.setattr("nomos.llm.council_agent.RETRY_DELAY_SECONDS", 0)
        backend = SequencedLLMBackend(
            [
                "I would use quorum here, but this is not JSON.",
                json.dumps(
                    {
                        "threshold": "quorum",
                        "reasoning": "Recovered with schema-valid JSON.",
                        "risk_level": "low",
                    }
                ),
            ]
        )
        chairman = ChairmanAgent(
            backend=backend,
            allow_quorum=True,
            temperature=0.1,
            max_tokens=768,
        )

        decision = await chairman.decide_threshold(test_intent)

        assert decision.threshold == "quorum"
        assert decision.risk_level == "low"
        assert backend.calls == 2
        assert backend.call_kwargs[0]["temperature"] == 0.1
        assert backend.call_kwargs[0]["max_tokens"] == 768
        assert "previous response was not accepted" in backend.call_messages[1][-1].content


class TestCreateBackend:
    """Tests for the backend factory."""

    def test_unknown_provider_rejected_by_enum(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            LLMBackendConfig(
                provider="unknown",
                model="test",
                api_key="test",
            )
