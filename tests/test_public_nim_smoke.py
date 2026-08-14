# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

from nomos.config.settings import CouncilMemberConfig, LLMBackendConfig, LLMProvider
from nomos.llm.council_agent import IntentEvaluation
from scripts import test_public_nim_smoke


def _settings(member_count: int):
    members = [
        CouncilMemberConfig(id=f"member-{index}", backend=f"backend-{index}")
        for index in range(member_count)
    ]
    backends = {
        f"backend-{index}": LLMBackendConfig(
            provider=LLMProvider.NIM,
            model=f"model-{index}",
            api_key="test",  # pragma: allowlist secret
        )
        for index in range(member_count)
    }
    return SimpleNamespace(members=members, llm_backends=backends)


def test_main_requires_nvidia_api_key(monkeypatch, capsys) -> None:
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["test_public_nim_smoke.py"])

    assert test_public_nim_smoke.main() == 2
    assert "NVIDIA_API_KEY is required" in capsys.readouterr().err


def test_run_smoke_rejects_config_without_members(monkeypatch, capsys) -> None:
    config = Path("custom-council.yaml")
    loaded_paths: list[str] = []

    def fake_load_config(path: str):
        loaded_paths.append(path)
        return _settings(0)

    monkeypatch.setattr(test_public_nim_smoke, "load_config", fake_load_config)

    assert asyncio.run(test_public_nim_smoke.run_smoke(config)) == 1
    assert loaded_paths == [str(config)]
    assert "config defines no members" in capsys.readouterr().err


def test_run_smoke_checks_every_configured_member_sequentially(monkeypatch) -> None:
    settings = _settings(4)
    for index, member in enumerate(settings.members):
        settings.llm_backends[member.backend] = settings.llm_backends[member.backend].model_copy(
            update={"request_timeout_seconds": 10.0 + index}
        )
    calls: list[str] = []
    backend_configs: list[LLMBackendConfig] = []
    agent_max_retries: list[int] = []
    active = 0
    max_active = 0

    class FakeAgent:
        def __init__(self, member_config, *, max_retries, **_kwargs):
            self.member_config = member_config
            agent_max_retries.append(max_retries)

        async def evaluate_intent(self, _intent):
            nonlocal active, max_active
            calls.append(self.member_config.id)
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0)
            active -= 1
            return IntentEvaluation(decision="approve", reasoning="compatible")

    def fake_create_backend(config):
        backend_configs.append(config)
        return object()

    monkeypatch.setattr(test_public_nim_smoke, "load_config", lambda _path: settings)
    monkeypatch.setattr(test_public_nim_smoke, "create_backend", fake_create_backend)
    monkeypatch.setattr(test_public_nim_smoke, "CouncilMemberAgent", FakeAgent)

    assert asyncio.run(test_public_nim_smoke.run_smoke(Path("config.yaml"))) == 0
    assert calls == [member.id for member in settings.members]
    assert max_active == 1
    assert [config.request_timeout_seconds for config in backend_configs] == [
        settings.llm_backends[member.backend].request_timeout_seconds for member in settings.members
    ]
    assert [config.sdk_max_retries for config in backend_configs] == [0] * len(settings.members)
    assert agent_max_retries == [0] * len(settings.members)


def test_run_smoke_continues_and_aggregates_failures(monkeypatch, capsys) -> None:
    settings = _settings(3)
    calls: list[str] = []

    class FakeAgent:
        def __init__(self, member_config, **_kwargs):
            self.member_config = member_config

        async def evaluate_intent(self, _intent):
            calls.append(self.member_config.id)
            if self.member_config.id == "member-1":
                return IntentEvaluation(decision="approve", reasoning="compatible")
            return IntentEvaluation(
                decision="deny",
                reasoning="provider unavailable",
                risk_concerns=["LLM evaluation failure"],
            )

    monkeypatch.setattr(test_public_nim_smoke, "load_config", lambda _path: settings)
    monkeypatch.setattr(test_public_nim_smoke, "create_backend", lambda _config: object())
    monkeypatch.setattr(test_public_nim_smoke, "CouncilMemberAgent", FakeAgent)

    assert asyncio.run(test_public_nim_smoke.run_smoke(Path("config.yaml"))) == 1

    output = capsys.readouterr()
    assert calls == [member.id for member in settings.members]
    assert "PASS: member-1 (model-1)" in output.out
    assert "FAIL: member-0 (model-0): provider unavailable" in output.err
    assert "FAIL: member-2 (model-2): provider unavailable" in output.err
