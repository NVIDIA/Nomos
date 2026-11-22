# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Authorization and transport boundaries for opt-in decision-model screening."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from nomos.config.settings import CouncilSettings, OPAConfig, SystemOneConfig
from nomos.level_0.types import Attestation, AttestationVector, Intent
from nomos.nat.functions import AuthorizeInput, CouncilService, ProposeIntentInput
from nomos.opa import OPAPolicyDecision
from nomos.system_one.client import (
    AttestationCatalog,
    CatalogQuestion,
    SystemOneClient,
    SystemOneEvaluationError,
)
from nomos.system_one.router import FastPathResult
from nomos.system_one.screening import evaluate_screening
from tests.test_opa_nat_integration import RecordingOPAClient

CATALOG = str(Path(__file__).resolve().parents[1] / "configs/attestation_questions.yaml")
DENY = "credential_exfiltration"
ASK = "reads_sensitive_paths"
RULES = {DENY: {"threshold": 0.8}, ASK: {"threshold": 0.8, "action": "requires_human_approval"}}


def _vector(scores):
    return AttestationVector(
        model_version="decision-model-1.0",
        catalog_version="test-1",
        attestations=[
            Attestation(
                question_id=question_id,
                category="security",
                qtype="noul",
                value=value,
                confidence=0.9,
                polarity="adverse",
            )
            for question_id, value in scores.items()
        ],
    )


class RecordingDecisionClient:
    """Return known answers without a network call; retain replay selections."""

    def __init__(self, scores=None, error=None):
        self.catalog = SimpleNamespace(version="test-1")
        self.scores = scores or {DENY: 0.1, ASK: 0.1}
        self.error = error
        self.calls = []

    async def evaluate(self, intent, *, question_ids=None):
        self.calls.append(question_ids)
        if self.error:
            raise self.error
        return _vector(
            {
                key: value
                for key, value in self.scores.items()
                if question_ids is None or key in question_ids
            }
        )

    async def aclose(self):
        pass


class ClosingOPAClient(RecordingOPAClient):
    async def aclose(self):
        pass


def _service(tmp_path, *, mode="enforce", rules=None, scores=None, error=None, opa=None):
    settings = CouncilSettings(
        council_id="screening-test",
        require_auth=False,
        llm_backends={},
        members=[],
        council_key_path=str(tmp_path),
        opa=OPAConfig(enabled=True),
        system_one=SystemOneConfig(
            enabled=True,
            catalog_path=CATALOG,
            screening={
                "enabled": True,
                "mode": mode,
                "rules": RULES if rules is None else rules,
            },
        ),
    )
    service = CouncilService(settings)
    client = RecordingDecisionClient(scores, error)
    service._system_one_client = client
    service._opa_client = ClosingOPAClient(opa or OPAPolicyDecision(allow=True))
    return service, client


def _request(*, policy_only=False):
    return AuthorizeInput(
        agent_id="agent-1",
        command="/bin/echo hello",
        working_directory="/tmp",
        description="Inspect the test workspace",
        justification="Validate screening",
        target_resource="/tmp",
        policy_only=policy_only,
    )


async def _unexpected(*args, **kwargs):
    pytest.fail("Screened calls must not reach Council actors")


def _no_council(monkeypatch, service):
    monkeypatch.setattr(service, "_run_code_review", _unexpected)
    monkeypatch.setattr(service, "_evaluate_fast_path", _unexpected)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scores", "expected"),
    [
        ({DENY: 0.8, ASK: 0.1}, "denied"),
        ({DENY: 0.1, ASK: 0.9}, "requires_human_approval"),
        ({DENY: 0.9, ASK: 0.9}, "denied"),
    ],
)
async def test_authorization_stops_before_council_or_writ(tmp_path, monkeypatch, scores, expected):
    service, client = _service(tmp_path, scores=scores)
    _no_council(monkeypatch, service)
    try:
        result = await service.authorize(_request())
        assert result.status == expected
        assert result.writ is None
        assert len(client.calls) == 1
        assert service.council.get_workflow(result.decision_id) is None
        entry = service.council.ledger.get_review_by_intent(result.decision_id)
        assert entry is not None
        audit = entry.payload["attestation"]["screening"]
        assert audit["enforced_decision"] == expected
        assert audit["catalog_version"] == "test-1"
        assert audit["selected_rules"][DENY] == {"threshold": 0.8, "action": "deny"}
        assert audit["mode"] == "enforce"
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_model_denial_precedes_opa_human_review(tmp_path, monkeypatch):
    service, client = _service(
        tmp_path,
        scores={DENY: 0.9, ASK: 0.9},
        opa=OPAPolicyDecision(
            allow=False,
            decision="requires_human_approval",
            reasons=["Human review required"],
        ),
    )
    _no_council(monkeypatch, service)
    try:
        result = await service.authorize(_request())
        assert result.status == "denied"
        assert client.calls == [None]
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_opa_hard_denial_never_calls_decision_model(tmp_path, monkeypatch):
    service, client = _service(
        tmp_path,
        opa=OPAPolicyDecision(
            allow=False,
            decision="deny",
            reasons=["Forbidden command"],
        ),
    )
    _no_council(monkeypatch, service)
    try:
        result = await service.authorize(_request())
        assert result.status == "denied"
        assert client.calls == []
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_model_error_is_not_a_human_approval_request(tmp_path, monkeypatch):
    service, _ = _service(tmp_path, error=SystemOneEvaluationError("Unavailable"))
    _no_council(monkeypatch, service)
    try:
        result = await service.authorize(_request())
        assert result.status == "error"
        assert result.writ is None
    finally:
        await service.aclose()


class CouncilReachedError(Exception):
    """Signal that normal review still owns an eligible operation."""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "scores", "error"),
    [
        ("enforce", {DENY: 0.1, ASK: 0.1}, None),
        ("shadow", {DENY: 0.9, ASK: 0.9}, None),
        ("shadow", None, SystemOneEvaluationError("Unavailable")),
    ],
)
async def test_clear_or_shadow_screening_preserves_review(
    tmp_path,
    monkeypatch,
    mode,
    scores,
    error,
):
    service, client = _service(tmp_path, mode=mode, scores=scores, error=error)
    seen = []

    async def no_fast_path(*args, **kwargs):
        return FastPathResult(False, False), FastPathResult(False, False)

    async def reached(intent):
        seen.append(intent.metadata["attestation"]["screening"])
        raise CouncilReachedError

    monkeypatch.setattr(service, "_evaluate_fast_path", no_fast_path)
    monkeypatch.setattr(service, "_run_code_review", reached)
    try:
        with pytest.raises(CouncilReachedError):
            await service.authorize(_request())
        assert client.calls == [None]
        assert seen[0]["enforced_decision"] == "continue"
        if error:
            assert seen[0]["decision"] == "error"
    finally:
        await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(("score", "expected"), [(0.1, "policy_allowed"), (0.9, "denied")])
async def test_approved_replay_only_requests_effective_denial_questions(
    tmp_path,
    monkeypatch,
    score,
    expected,
):
    service, client = _service(tmp_path, scores={DENY: score, ASK: 0.99})
    _no_council(monkeypatch, service)
    try:
        result = await service.authorize(_request(policy_only=True))
        assert result.status == expected
        assert result.writ is None
        assert client.calls == [{DENY}]
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_replay_without_deny_rules_does_not_call_model(tmp_path, monkeypatch):
    service, client = _service(tmp_path, rules={ASK: RULES[ASK]})
    _no_council(monkeypatch, service)
    try:
        result = await service.authorize(_request(policy_only=True))
        assert result.status == "policy_allowed"
        assert client.calls == []
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_replay_model_error_stops_execution(tmp_path, monkeypatch):
    service, client = _service(tmp_path, error=SystemOneEvaluationError("Unavailable"))
    _no_council(monkeypatch, service)
    try:
        result = await service.authorize(_request(policy_only=True))
        assert result.status == "error"
        assert result.writ is None
        assert client.calls == [{DENY}]
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_legacy_proposal_cannot_bypass_screening(tmp_path, monkeypatch):
    service, client = _service(tmp_path, scores={DENY: 0.9, ASK: 0.1})
    _no_council(monkeypatch, service)
    try:
        result = await service.propose_intent(
            ProposeIntentInput(
                agent_id="agent-1",
                command="/bin/echo hello",
                working_directory="/tmp",
                description="Inspect a directory",
                justification="Validate proposal screening",
                target_resource="/tmp",
            )
        )
        assert result.success is False
        assert result.state == "denied"
        assert service.council.get_workflow(result.intent_id) is None
        assert client.calls == [None]
    finally:
        await service.aclose()


def _client(*, mode="enforce", enabled=True):
    catalog = AttestationCatalog(
        version="test-1",
        questions=[
            CatalogQuestion(id=DENY, category="security", qtype="noul"),
            CatalogQuestion(id=ASK, category="security", qtype="noul"),
        ],
    )
    config = SystemOneConfig(
        enabled=True,
        api_key="test-authentication",  # pragma: allowlist secret -- offline test fixture
        base_url="https://backend.example",
        screening={"enabled": enabled, "mode": mode, "rules": RULES},
    )
    return SystemOneClient(config, catalog)


def _intent():
    return Intent.create(
        agent_id="agent-1",
        description="Inspect test files",
        command="/bin/echo hello",
        target_resource="/tmp",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        {"type": "noul", "noul": True},
        {"type": "noul", "noul": "0.1"},
        {"type": "choice", "noul": 0.1},
        {"noul": 0.1},
        {"type": "noul", "noul": float("nan")},
        {"type": "noul", "noul": 1.1},
        {"type": "noul", "noul": -0.1},
        {"type": "noul", "noul": float("inf")},
        {"type": "noul", "value": 0.1},
        None,
    ],
)
async def test_transport_rejects_malformed_screened_answers(monkeypatch, answer):
    client = _client()

    async def response(body):
        return {
            "model": "decision-model-1.0",
            "answers": {
                DENY: answer,
                ASK: {"type": "noul", "noul": 0.1},
            },
        }

    monkeypatch.setattr(client, "_post", response)
    with pytest.raises(SystemOneEvaluationError):
        await client.evaluate(_intent())


@pytest.mark.asyncio
async def test_overlapping_full_and_replay_requests_keep_independent_catalogs(monkeypatch):
    client = _client()
    requests = []
    both_started = asyncio.Event()

    async def response(body):
        ids = set(body["questions"])
        requests.append(ids)
        if len(requests) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=2)
        return {
            "model": "decision-model-1.0",
            "answers": {key: {"type": "noul", "noul": 0.1} for key in ids},
        }

    monkeypatch.setattr(client, "_post", response)
    full, replay = await asyncio.gather(
        client.evaluate(_intent()),
        client.evaluate(_intent(), question_ids={DENY}),
    )
    assert {answer.question_id for answer in full.attestations} == {DENY, ASK}
    assert {answer.question_id for answer in replay.attestations} == {DENY}
    assert requests == [{DENY, ASK}, {DENY}]
    assert {question.id for question in client.catalog.questions} == {DENY, ASK}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (0.1, "requires_human_approval"),
        (0.9, "denied"),
    ],
)
async def test_replay_preserves_opa_ask_after_checking_only_hard_denials(
    tmp_path,
    monkeypatch,
    score,
    expected,
):
    """The worker satisfies the original ask using its verified saved approval."""
    service, client = _service(
        tmp_path,
        scores={DENY: score, ASK: 0.99},
        opa=OPAPolicyDecision(
            allow=False,
            decision="requires_human_approval",
            reasons=["Exact-operation approval"],
        ),
    )
    _no_council(monkeypatch, service)
    try:
        result = await service.authorize(_request(policy_only=True))
        assert result.status == expected
        assert result.writ is None
        assert client.calls == [{DENY}]
    finally:
        await service.aclose()


def test_screening_catalog_failure_is_a_startup_error(tmp_path):
    """An enabled enforcement policy cannot silently disappear on bad input."""
    settings = CouncilSettings(
        council_id="screening-startup-test",
        require_auth=False,
        llm_backends={},
        members=[],
        council_key_path=str(tmp_path),
        system_one=SystemOneConfig(
            enabled=True,
            catalog_path=str(tmp_path / "missing-catalog.yaml"),
            screening={"enabled": True, "mode": "enforce", "rules": RULES},
        ),
    )
    with pytest.raises(ValueError, match="Could not initialize decision-model screening"):
        CouncilService(settings)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        {"type": "noul", "noul": "0.1"},
        {"noul": 0.1},
    ],
)
async def test_shadow_preserves_legacy_vector_but_marks_invalid_screening(monkeypatch, answer):
    client = _client(mode="shadow")

    async def response(body):
        return {
            "model": "decision-model-1.0",
            "answers": {
                DENY: answer,
                ASK: {"type": "noul", "noul": 0.1},
            },
        }

    monkeypatch.setattr(client, "_post", response)
    vector = await client.evaluate(_intent())
    assert vector.by_id(DENY).value == 0.1
    assert vector.by_id(DENY).detail["screening_answer_valid"] is False
    result = evaluate_screening(vector, client._config.screening)
    assert result.decision == "error"
    assert result.enforced_decision == "continue"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "enabled", "reject"),
    [
        ("enforce", True, True),
        ("shadow", True, False),
        ("enforce", False, False),
    ],
)
async def test_duplicate_json_answers_are_rejected_only_when_enforcing(mode, enabled, reject):
    client = _client(mode=mode, enabled=enabled)
    raw = b'{"answers":{"test":{"type":"noul","noul":0.99,"noul":0.01}}}'

    def response(request):
        return httpx.Response(200, content=raw, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as transport:
        client._client = transport
        if reject:
            with pytest.raises(SystemOneEvaluationError):
                await client._post({"questions": {}})
        else:
            payload = await client._post({"questions": {}})
            assert payload["answers"]["test"]["noul"] == 0.01


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat_denial", [False, True])
async def test_enforced_batches_cannot_overwrite_another_questions_answer(
    monkeypatch, repeat_denial
):
    """A later batch cannot replace a high denial probability with a low one."""
    client = _client()
    client._config.max_questions_per_request = 1

    async def response(body):
        question_id = next(iter(body["questions"]))
        answers = {question_id: {"type": "noul", "noul": 0.99 if question_id == DENY else 0.1}}
        if repeat_denial and question_id == ASK:
            answers[DENY] = {"type": "noul", "noul": 0.01}
        return {"model": "decision-model-1.0", "answers": answers}

    monkeypatch.setattr(client, "_post", response)
    try:
        if repeat_denial:
            with pytest.raises(SystemOneEvaluationError, match="requested question batch"):
                await client.evaluate(_intent())
        else:
            vector = await client.evaluate(_intent())
            assert vector.by_id(DENY).value == 0.99
    finally:
        await client.aclose()
