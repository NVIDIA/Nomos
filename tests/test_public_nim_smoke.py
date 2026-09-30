# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import openai
import pytest

from nomos.config.settings import (
    ChairmanConfig,
    CodeReviewConfig,
    CouncilMemberConfig,
    CouncilSettings,
    LLMBackendConfig,
    LLMProvider,
)
from nomos.llm.council_agent import ChairmanDecision
from scripts import test_public_nim_smoke as smoke


def _settings(member_count=2, *, enabled=True):
    return CouncilSettings(
        members=[
            CouncilMemberConfig(id=f"member-{i}", backend=f"backend-{i}")
            for i in range(member_count)
        ],
        llm_backends={
            f"backend-{i}": LLMBackendConfig(
                provider=LLMProvider.NIM,
                model=f"model-{i}",
                api_key="test",  # pragma: allowlist secret
                base_url="https://example.test/v1",
                temperature=0.2,
                max_tokens=512 + i,
                request_timeout_seconds=5.0 + i,
                sdk_max_retries=4,
            )
            for i in range(max(member_count, 1))
        },
        chairman=ChairmanConfig(enabled=enabled, backend="backend-0"),
        code_review=CodeReviewConfig(enabled=enabled, backend="backend-0"),
        llm_retry_budget_seconds=0.1,
        llm_max_retry_after_seconds=0.01,
        llm_max_retries=5,
    )


def _config(monkeypatch, tmp_path, settings):
    path = tmp_path / "custom-council.yaml"
    path.write_text("test config\n")
    loaded = []

    def load(value):
        loaded.append(value)
        return settings

    monkeypatch.setattr(smoke, "load_config", load)
    return path, loaded


def _response(kwargs, *, decision="deny"):
    system = kwargs["messages"][0]["content"]
    if "You are the Chairman" in system:
        return {"threshold": "quorum", "risk_level": "low", "reasoning": "Harmless read."}
    if system.startswith("You are the Code Review council member"):
        attachment = smoke.smoke_intent(packaged=True).packaged_files[0]
        assert attachment.sha256 in kwargs["messages"][1]["content"]
        assert 'print("Nomos compatibility check")' in kwargs["messages"][1]["content"]
        return {
            "decision": decision,
            "reviewed_all_content": True,
            "claim_match": "yes",
            "overall_risk": "low",
            "reasoning": "Prints a fixed message. No imports or other operations.",
            "files": [
                {
                    "path": attachment.path,
                    "sha256": attachment.sha256,
                    "mode": attachment.mode,
                    "review_status": "complete",
                    "matches_claimed_behavior": "yes",
                    "observed_operations": ["prints fixed message"],
                    "risk_findings": [],
                    "evidence": ["line 1: print"],
                }
            ],
            "risk_concerns": [],
            "conditions": [],
            "guidance": "",
        }
    return {"decision": decision, "reasoning": "Valid compatibility response."}


def _transport(monkeypatch, behavior=None):
    """Mock only the SDK transport; use real NIM backend and role parsers."""
    calls, clients = [], []
    active = 0
    max_active = 0

    class Client:
        def __init__(self, **kwargs):
            clients.append(kwargs)
            self.record = kwargs
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        async def close(self):
            self.record["closed"] = True

        async def create(self, **kwargs):
            nonlocal active, max_active
            index = len(calls)
            calls.append(kwargs)
            active += 1
            max_active = max(active, max_active)
            try:
                await asyncio.sleep(0)
                content = await behavior(index, kwargs) if behavior else _response(kwargs)
                if not isinstance(content, str):
                    content = json.dumps(content)
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content=content), finish_reason="stop"
                        )
                    ],
                    model="returned-" + kwargs["model"],
                    usage=None,
                    id="test-response",
                )
            finally:
                active -= 1

    monkeypatch.setattr(openai, "AsyncOpenAI", Client)
    return calls, clients, lambda: max_active


def test_main_requires_api_keys_referenced_by_selected_config(monkeypatch, tmp_path, capsys):
    config = tmp_path / "anthropic-only.yaml"
    config.write_text(
        "llm_backends:\n"
        "  claude:\n"
        "    api_key: ${ANTHROPIC_API_KEY}\n"
        "  openai:\n"
        "    api_key: ${OPENAI_API_KEY}\n"
    )
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["smoke", "--config", str(config)])
    assert smoke.main() == 2
    error = capsys.readouterr().err
    assert "ANTHROPIC_API_KEY, OPENAI_API_KEY" in error
    assert "NVIDIA_API_KEY" not in error


def test_main_accepts_non_nvidia_config_without_nvidia_key(monkeypatch, tmp_path):
    config = tmp_path / "anthropic-only.yaml"
    config.write_text("llm_backends:\n  claude:\n    api_key: ${ANTHROPIC_API_KEY}\n")
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    run_smoke = AsyncMock(return_value=0)
    monkeypatch.setattr(smoke, "run_smoke", run_smoke)
    monkeypatch.setattr(sys, "argv", ["smoke", "--config", str(config)])

    assert smoke.main() == 0
    run_smoke.assert_awaited_once_with(config)


def test_custom_config_argument(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["smoke", "--config", "custom.yaml"])
    assert str(smoke.parse_args().config) == "custom.yaml"


def test_missing_config_is_safe(tmp_path, capsys):
    assert asyncio.run(smoke.run_smoke(tmp_path / "missing")) == 1
    assert "FileNotFoundError" in capsys.readouterr().err


def test_no_members(monkeypatch, tmp_path, capsys):
    path, loaded = _config(monkeypatch, tmp_path, _settings(0))
    assert asyncio.run(smoke.run_smoke(path)) == 1
    assert loaded == [str(path)]
    assert "config defines no members" in capsys.readouterr().err


@pytest.mark.parametrize("enabled", [True, False])
def test_all_roles_configured_parameters_and_sequencing(monkeypatch, tmp_path, capsys, enabled):
    settings = _settings(3, enabled=enabled)
    before = settings.model_dump()
    path, loaded = _config(monkeypatch, tmp_path, settings)
    calls, clients, max_active = _transport(monkeypatch)
    assert asyncio.run(smoke.run_smoke(path)) == 0
    assert loaded == [str(path)]
    assert settings.model_dump() == before
    assert [call["model"] for call in calls] == ["model-0", "model-1", "model-2"] + (
        ["model-0", "model-0"] if enabled else []
    )
    assert [call["max_tokens"] for call in calls] == [512, 513, 514] + (
        [512, 2048] if enabled else []
    )
    assert all(call["temperature"] == 0.2 for call in calls)
    assert [client["timeout"] for client in clients] == [5.0, 6.0, 7.0] + (
        [5.0, 5.0] if enabled else []
    )
    assert all(client["max_retries"] == 0 for client in clients)
    assert all(client["closed"] for client in clients)
    assert max_active() == 1
    output = capsys.readouterr()
    assert output.err == ""
    assert hashlib.sha256(path.read_bytes()).hexdigest() in output.out
    assert '"returned_model": "returned-model-0"' in output.out
    assert '"decision": "deny"' in output.out  # A legitimate denial is compatible.
    assert output.out.count("PASS:") == len(calls)
    if enabled:
        assert '"threshold": "unanimous"' in output.out  # Preserve configured policy.


@pytest.mark.parametrize("failure", ["malformed", "empty", "invalid", "timeout", "http"])
def test_failures_aggregate_across_all_roles_without_retries(
    monkeypatch, tmp_path, capsys, failure
):
    path, _ = _config(monkeypatch, tmp_path, _settings(2))

    async def behavior(index, kwargs):
        if index == 1:  # Still reach a healthy member after the first failure.
            return _response(kwargs)
        if failure == "malformed":
            return "bad response secret-body"
        if failure == "empty":
            return {}
        if failure == "invalid":
            result = _response(kwargs)
            result["threshold" if "threshold" in result else "decision"] = "nonsense"
            return result
        if failure == "timeout":
            await asyncio.sleep(1)
        response = httpx.Response(401, request=httpx.Request("POST", "https://example.test"))
        raise openai.AuthenticationError("secret-body", response=response, body="secret-body")

    calls, clients, max_active = _transport(monkeypatch, behavior)
    prior_logging = logging.root.manager.disable
    assert asyncio.run(smoke.run_smoke(path)) == 1
    assert logging.root.manager.disable == prior_logging
    assert len(calls) == 4  # One call per role, even with retry settings > 0.
    assert all(client["closed"] for client in clients)
    assert max_active() == 1
    output = capsys.readouterr()
    assert output.out.count("PASS:") == 1
    assert output.err.count("FAIL:") == 3
    assert "secret-body" not in output.out + output.err
    expected_labels = {
        "malformed": {"ParseFailureOrFallback": 3},
        "empty": {"InvalidResponse": 1, "ParseFailureOrFallback": 2},
        "invalid": {"InvalidResponse": 2, "ParseFailureOrFallback": 1},
        "timeout": {"DeadlineExceeded": 3},
        "http": {"AuthenticationError": 3},
    }[failure]
    for label, count in expected_labels.items():
        assert output.err.count(f'"error": "{label}"') == count
    if failure == "http":
        assert '"http_status": 401' in output.err
    if failure == "timeout":
        assert "DeadlineExceeded" in output.err


def test_chairman_fallback_without_inference_is_failure(monkeypatch, tmp_path, capsys):
    path, _ = _config(monkeypatch, tmp_path, _settings(1))
    calls, _, _ = _transport(monkeypatch)

    async def fallback(self, intent):
        return ChairmanDecision(threshold="unanimous", reasoning="Deterministic result")

    monkeypatch.setattr(smoke.ChairmanAgent, "decide_threshold", fallback)
    assert asyncio.run(smoke.run_smoke(path)) == 1
    assert len(calls) == 2
    assert "ExpectedOneInferenceResponse" in capsys.readouterr().err


def test_enabled_missing_backend_continues(monkeypatch, tmp_path, capsys):
    settings = _settings(1)
    settings.chairman.backend = "missing"
    path, _ = _config(monkeypatch, tmp_path, settings)
    calls, _, _ = _transport(monkeypatch)
    assert asyncio.run(smoke.run_smoke(path)) == 1
    assert len(calls) == 2
    assert '"role": "chairman"' in capsys.readouterr().err


def test_endpoint_redacts_credentials_and_query():
    config = (
        _settings(1)
        .llm_backends["backend-0"]
        .model_copy(
            update={
                "base_url": "https://u:p@example.test/v1?k=s#t"  # pragma: allowlist secret
            }
        )
    )
    assert smoke.safe_endpoint(config) == "https://example.test/v1"
