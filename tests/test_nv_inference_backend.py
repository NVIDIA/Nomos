# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


"""Tests for the NVIDIA inference LLM backend."""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from nomos.llm.base import LLMMessage, LLMRole
from nomos.llm.nv_inference_backend import NVInferenceBackend


class _FakeCompletions:
    def __init__(self):
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            id="chatcmpl-test",
            model=kwargs["model"],
            choices=[SimpleNamespace(message=SimpleNamespace(content="{}"), finish_reason="stop")],
            usage=None,
        )


class _FakeOpenAIClient:
    last_client: _FakeOpenAIClient | None = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.chat = SimpleNamespace(completions=_FakeCompletions())
        _FakeOpenAIClient.last_client = self


@pytest.fixture
def fake_openai(monkeypatch):
    module = types.ModuleType("openai")
    module.AsyncOpenAI = _FakeOpenAIClient
    monkeypatch.setitem(sys.modules, "openai", module)
    _FakeOpenAIClient.last_client = None
    return _FakeOpenAIClient


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model",
    [
        "aws/anthropic/bedrock-claude-opus-4-7",
        "openai/openai/gpt-5.5",
    ],
)
async def test_nv_inference_omits_temperature_for_models_that_reject_it(fake_openai, model):
    backend = NVInferenceBackend(api_key="test", model=model)

    await backend.complete(
        [LLMMessage(role=LLMRole.USER, content="hello")],
        temperature=0.0,
        max_tokens=1024,
    )

    client = fake_openai.last_client
    assert client is not None
    call = client.chat.completions.calls[0]
    assert call["model"] == model
    assert "temperature" not in call
    assert call["max_tokens"] == 1024


@pytest.mark.asyncio
async def test_nv_inference_sends_temperature_for_other_models(fake_openai):
    backend = NVInferenceBackend(api_key="test", model="azure/openai/gpt-5.4")

    await backend.complete(
        [LLMMessage(role=LLMRole.USER, content="hello")],
        temperature=0.0,
        max_tokens=1024,
    )

    client = fake_openai.last_client
    assert client is not None
    call = client.chat.completions.calls[0]
    assert call["model"] == "azure/openai/gpt-5.4"
    assert call["temperature"] == 0.0


def test_nv_inference_passes_timeout_and_retries_to_client(fake_openai):
    NVInferenceBackend(api_key="test", timeout=30.0, max_retries=0)  # pragma: allowlist secret

    client = fake_openai.last_client
    assert client is not None
    assert client.kwargs["timeout"] == 30.0
    assert client.kwargs["max_retries"] == 0


def test_nv_inference_omits_timeout_kwargs_when_unset(fake_openai):
    NVInferenceBackend(api_key="test")  # pragma: allowlist secret

    client = fake_openai.last_client
    assert client is not None
    assert "timeout" not in client.kwargs
    assert "max_retries" not in client.kwargs
