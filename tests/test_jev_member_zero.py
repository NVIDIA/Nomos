# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Jev as member zero: the chairman, the read-only fast path, and the council prompt.

Jev itself is stubbed: these tests hand the service a ready-made attestation
vector. The invariants under test are that every relaxation (quorum, fast path)
needs ALL of its conditions, and that any missing or malformed answer falls to
the safe outcome.
"""

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from nomos.auth.authenticator import AgentIdentity
from nomos.auth.writ_factory import WritFactory
from nomos.config.settings import (
    ChairmanConfig,
    CouncilSettings,
    JevChairmanConfig,
    JevConfig,
    JevFastPathConfig,
    OPAConfig,
)
from nomos.jev import JevChairmanAgent, JevRoutingError, chairman_decision, evaluate_fast_path
from nomos.level_0.types import Attestation, AttestationVector, CouncilMember, Intent
from nomos.level_1.ledger import LedgerEntryType
from nomos.llm.council_agent import IntentEvaluation, _format_intent_for_review
from nomos.nat.functions import AuthorizeInput, CouncilService
from nomos.opa import OPAPolicyDecision


def _vector(
    *,
    read_only: float = 0.99,
    danger: float = 0.02,
    risk: str = "low",
    confidence: float = 0.97,
    confidence_source: str = "reported",
) -> AttestationVector:
    return AttestationVector(
        model_version="jev-1.13.0",
        catalog_version="0.6.0",
        attestations=[
            Attestation(
                question_id="read_only_operation",
                category="risk",
                qtype="noul",
                value=read_only,
                confidence=abs(2 * read_only - 1),
                tier="triage",
                polarity="safe",
                detail={"confidence_source": "certainty_proxy"},
            ),
            Attestation(
                question_id="discloses_sensitive_content",
                category="risk",
                qtype="noul",
                value=danger,
                confidence=abs(2 * danger - 1),
                tier="triage",
                detail={"confidence_source": "certainty_proxy"},
            ),
            Attestation(
                question_id="risk_level",
                category="risk",
                qtype="choice",
                value=risk,
                confidence=confidence,
                tier="risk",
                detail={"confidence_source": confidence_source},
            ),
            Attestation(
                question_id="prompt_injection",
                category="injection",
                qtype="noul",
                value=0.01,
                confidence=0.98,
                action="deny-candidate",
                detail={"confidence_source": "certainty_proxy"},
            ),
        ],
    )


def _make_intent(command: str = "/usr/bin/kubectl get pods -n payments", **overrides) -> Intent:
    kwargs: dict[str, Any] = {
        "agent_id": "app-service",
        "action_type": "system.info",
        "description": "List the pods in the payments namespace",
        "target_resource": "/workspace/payments",
        "command": command,
        "justification": "Checking rollout state",
        "target_environment": "staging",
    }
    kwargs.update(overrides)
    return Intent.create(**kwargs)


def _with_vector(intent: Intent, vector: AttestationVector | None) -> Intent:
    intent._attestation = vector
    return intent


_ALL_CLEAR = {
    "authenticated": True,
    "opa_allowed": True,
    "policy_allows": True,
    "floor_fired": False,
    "has_packaged_files": False,
}


class TestChairmanDecision:
    def test_all_conditions_met_grants_quorum(self):
        decision = chairman_decision(_vector(), JevChairmanConfig())
        assert decision.threshold == "quorum"
        assert decision.risk_level == "low"
        assert "jev-1.13.0" in decision.reasoning

    @pytest.mark.parametrize(
        "vector",
        [
            _vector(confidence=0.89),
            _vector(read_only=0.89),
            _vector(risk="medium"),
            _vector(risk="critical", confidence=1.0),
        ],
    )
    def test_any_condition_missing_is_unanimous(self, vector):
        decision = chairman_decision(vector, JevChairmanConfig())
        assert decision.threshold == "unanimous"
        assert decision.risk_level == vector.by_id("risk_level").value

    @pytest.mark.parametrize("risk", ["low", "medium"])
    def test_unsure_low_or_medium_is_reported_high(self, risk):
        """openjev-4b answered 'low' at confidence 0.26 for an rm -rf in production."""
        decision = chairman_decision(_vector(risk=risk, confidence=0.26), JevChairmanConfig())
        assert decision.risk_level == "high"
        assert decision.threshold == "unanimous"
        assert "low confidence" in decision.reasoning

    def test_unsure_critical_stays_critical(self):
        decision = chairman_decision(_vector(risk="critical", confidence=0.3), JevChairmanConfig())
        assert decision.risk_level == "critical"

    @pytest.mark.parametrize(
        "vector",
        [
            _vector(risk="negligible"),
            _vector(confidence_source="certainty_proxy"),
            AttestationVector(model_version="m", catalog_version="c"),
        ],
    )
    def test_malformed_vector_raises(self, vector):
        with pytest.raises(JevRoutingError):
            chairman_decision(vector, JevChairmanConfig())


class TestFastPathRouter:
    def test_all_conditions_met_is_eligible(self):
        result = evaluate_fast_path(_vector(), JevFastPathConfig(enabled=True), **_ALL_CLEAR)
        assert result.eligible and result.approved
        assert result.failed == []
        assert result.values["read_only_operation"] == pytest.approx(0.99)
        assert result.values["risk_level"] == {"choice": "low", "confidence": 0.97}

    def test_disabled_still_records_a_shadow_verdict(self):
        result = evaluate_fast_path(_vector(), JevFastPathConfig(), **_ALL_CLEAR)
        assert result.eligible is True
        assert result.approved is False
        assert result.to_dict()["enabled"] is False

    @pytest.mark.parametrize(
        ("override", "reason"),
        [
            ({"authenticated": False}, "not authenticated"),
            ({"opa_allowed": False}, "OPA"),
            ({"policy_allows": False}, "policy rule"),
            ({"floor_fired": True}, "floor"),
            ({"has_packaged_files": True}, "packaged files"),
        ],
    )
    def test_each_deterministic_exclusion_blocks(self, override, reason):
        conditions = {**_ALL_CLEAR, **override}
        result = evaluate_fast_path(_vector(), JevFastPathConfig(enabled=True), **conditions)
        assert result.approved is False
        assert any(reason in failure for failure in result.failed)

    @pytest.mark.parametrize(
        ("vector", "reason"),
        [
            (_vector(read_only=0.94), "read_only_operation 0.94 < 0.95"),
            (_vector(danger=0.21), "discloses_sensitive_content 0.21 > 0.20"),
            (_vector(risk="medium"), "risk_level is medium"),
            (_vector(confidence=0.89), "risk_level confidence 0.89 < 0.90"),
            (_vector(confidence_source="unavailable"), "not reported"),
            (None, "no Jev vector"),
        ],
    )
    def test_each_jev_threshold_blocks(self, vector, reason):
        result = evaluate_fast_path(vector, JevFastPathConfig(enabled=True), **_ALL_CLEAR)
        assert result.approved is False
        assert any(reason in failure for failure in result.failed), result.failed


class TestJevChairmanAgent:
    def _chairman(self, *, allow_quorum: bool = True) -> JevChairmanAgent:
        return JevChairmanAgent(
            JevChairmanConfig(), model_version="jev-1.13.0", allow_quorum=allow_quorum
        )

    @pytest.mark.asyncio
    async def test_reads_the_service_vector(self):
        decision = await self._chairman().decide_threshold(_with_vector(_make_intent(), _vector()))
        assert decision.threshold == "quorum"
        assert decision.risk_level == "low"

    @pytest.mark.asyncio
    async def test_no_vector_is_unanimous_high(self):
        decision = await self._chairman().decide_threshold(_make_intent())
        assert decision.threshold == "unanimous"
        assert decision.risk_level == "high"
        assert "failed" in decision.reasoning

    @pytest.mark.asyncio
    async def test_agent_metadata_is_not_a_vector(self):
        """Only the service-set private slot counts, never intent metadata."""
        intent = _make_intent(metadata={"attestation": _vector().to_dict()})
        decision = await self._chairman().decide_threshold(intent)
        assert decision.threshold == "unanimous"
        assert decision.risk_level == "high"

    @pytest.mark.asyncio
    async def test_quorum_clamped_when_deployment_disallows_it(self):
        chairman = self._chairman(allow_quorum=False)
        decision = await chairman.decide_threshold(_with_vector(_make_intent(), _vector()))
        assert decision.threshold == "unanimous"
        assert decision.risk_level == "low"

    @pytest.mark.asyncio
    async def test_deterministic_floor_beats_a_confident_jev(self):
        """Self-advocating prose says read-only; the command redirects to disk."""
        intent = _make_intent(
            "/bin/cat /etc/hostname > /workspace/out.txt",
            action_type="file.read",
            blast_radius="read-only, no state change",
        )
        decision = await self._chairman().decide_threshold(_with_vector(intent, _vector()))
        assert decision.threshold == "unanimous"
        assert decision.risk_level == "high"
        assert "Jev assessment" in decision.reasoning

    @pytest.mark.asyncio
    async def test_missing_permission_short_circuits(self):
        result = await self._chairman().pre_authorize(
            _with_vector(_make_intent(), _vector()), {"file.read"}, {"system.admin"}
        )
        assert result.authorized is False

    def test_provider_name_carries_pinned_version(self):
        assert self._chairman().provider_name == "jev:jev-1.13.0"


class TestConfig:
    def test_defaults_are_off(self):
        assert JevConfig().enabled is False
        assert JevConfig().fast_path.enabled is False
        assert ChairmanConfig().provider == "llm"

    def test_jev_chairman_requires_jev_enabled(self):
        with pytest.raises(ValidationError, match=r"requires jev\.enabled"):
            CouncilSettings(chairman=ChairmanConfig(enabled=True, provider="jev"))

    def test_unknown_chairman_provider_rejected(self):
        with pytest.raises(ValidationError, match="provider"):
            ChairmanConfig(provider="gpt")

    @pytest.mark.parametrize("alias", ["jev-latest", "jev-preview"])
    def test_floating_model_alias_rejected(self, alias):
        with pytest.raises(ValidationError, match="concrete version"):
            JevConfig(provider="typesafe", api_key="k", model_version=alias)


class TestCouncilPrompt:
    def test_full_vector_rendered_with_untrusted_framing(self):
        prompt = _format_intent_for_review(_with_vector(_make_intent(), _vector()))
        assert "## Pre-Council Attestations (Jev)" in prompt
        assert "agent's own wording can move these numbers" in prompt
        assert "read_only_operation [triage, safe, informational]: 0.99" in prompt
        assert "risk_level [risk, adverse, informational]: low, confidence 0.97" in prompt
        assert "prompt_injection [attestation, adverse, deny-candidate]: 0.01" in prompt

    def test_no_section_without_a_vector(self):
        assert "Pre-Council Attestations" not in _format_intent_for_review(_make_intent())


class _StubJev:
    def __init__(self, vector: AttestationVector):
        self.vector = vector
        self.calls: list[Intent] = []

    async def evaluate(self, intent: Intent) -> AttestationVector:
        self.calls.append(intent)
        return self.vector


class _AllowOPA:
    async def evaluate(self, _input_doc):
        return OPAPolicyDecision(allow=True)


def _service(
    tmp_path,
    monkeypatch,
    *,
    vector: AttestationVector,
    fast_path: bool,
    jev_chairman: bool = False,
) -> tuple[CouncilService, list[Intent]]:
    chairman = (
        ChairmanConfig(enabled=True, provider="jev", allow_quorum=True)
        if jev_chairman
        else ChairmanConfig()
    )
    service = CouncilService(
        CouncilSettings(
            council_id="test",
            require_auth=False,
            members=[],
            opa=OPAConfig(enabled=True),
            jev=JevConfig(
                enabled=True,
                provider="typesafe",
                api_key="k",  # pragma: allowlist secret
                fast_path=JevFastPathConfig(enabled=fast_path),
            ),
            chairman=chairman,
            council_key_path=str(tmp_path / "keys"),
        )
    )
    service._jev_client = _StubJev(vector)
    service._opa_client = _AllowOPA()
    identity = AgentIdentity(
        agent_id="app-service",
        display_name="App",
        permissions={"shell:read", "shell:write"},
        councils={"test"},
    )

    async def resolve(*_args):
        return identity, ""

    monkeypatch.setattr(service, "_resolve_request_agent", resolve)

    reviewed: list[Intent] = []

    async def evaluate(intent):
        reviewed.append(intent)
        return IntentEvaluation(decision="approve", reasoning="Looks fine")

    service._agents.append(
        SimpleNamespace(
            member_id="member",
            display_name="Member",
            provider_name="fake",
            evaluate_intent=evaluate,
        )
    )
    service.council.register_council_member(
        CouncilMember(member_id="member", public_key_hex="", display_name="Member", weight=1)
    )
    return service, reviewed


def _request(**overrides) -> AuthorizeInput:
    kwargs: dict[str, Any] = {
        "agent_id": "app-service",
        "council_id": "test",
        "command": "/usr/bin/kubectl get pods -n payments",
        "working_directory": "/workspace",
        "description": "List the pods in the payments namespace",
        "target_resource": "/workspace/payments",
        "justification": "Checking rollout state",
        "blast_radius": "None, read-only listing",
    }
    kwargs.update(overrides)
    return AuthorizeInput(**kwargs)


def _review_record(service: CouncilService) -> dict:
    ledger = service.council.ledger
    entries = [ledger.get(i) for i in range(ledger.length())]
    (record,) = [e for e in entries if e.entry_type == LedgerEntryType.INTENT_REVIEW_RECORDED]
    return record.payload


class TestServiceFastPath:
    @pytest.mark.asyncio
    async def test_eligible_read_mints_without_the_council(self, tmp_path, monkeypatch):
        service, reviewed = _service(tmp_path, monkeypatch, vector=_vector(), fast_path=True)
        result = await service.authorize(_request())

        assert result.status == "approved", result.error
        assert reviewed == []
        assert result.chairman.threshold == "fast_path"
        writ = WritFactory.deserialize_writ(result.writ)
        assert writ.threshold_used == "fast_path"
        assert writ.member_votes == []
        assert writ.verify(service._active_signer().public_key_bytes())

        payload = _review_record(service)
        assert payload["final"]["decision"] == "approve"
        assert payload["attestation"]["fast_path"]["approved"] is True
        assert payload["attestation"]["model_version"] == "jev-1.13.0"

    @pytest.mark.asyncio
    async def test_disabled_fast_path_goes_to_council_and_records_shadow(
        self, tmp_path, monkeypatch
    ):
        service, reviewed = _service(tmp_path, monkeypatch, vector=_vector(), fast_path=False)
        result = await service.authorize(_request())

        assert result.status == "approved", result.error
        assert len(reviewed) == 1
        # Members see the vector in their prompt.
        assert reviewed[0]._attestation is not None
        fast_path = _review_record(service)["attestation"]["fast_path"]
        assert fast_path == {**fast_path, "enabled": False, "eligible": True, "approved": False}

    @pytest.mark.asyncio
    async def test_named_policy_rule_blocks_the_fast_path(self, tmp_path, monkeypatch):
        """The production quorum rule asks for review, so the council convenes."""
        service, reviewed = _service(tmp_path, monkeypatch, vector=_vector(), fast_path=True)
        result = await service.authorize(_request(target_resource="prod/payments"))

        # The rule wants weight 2 and this council has one member: what matters
        # is that the council, not the fast path, decided.
        assert result.writ is None
        assert len(reviewed) == 1
        failed = _review_record(service)["attestation"]["fast_path"]["failed"]
        assert "council policy rule requires review" in failed

    @pytest.mark.asyncio
    async def test_low_read_only_score_goes_to_council(self, tmp_path, monkeypatch):
        service, reviewed = _service(
            tmp_path, monkeypatch, vector=_vector(read_only=0.88), fast_path=True
        )
        result = await service.authorize(_request())
        assert result.status == "approved", result.error
        assert len(reviewed) == 1

    @pytest.mark.asyncio
    async def test_jev_chairman_reads_the_same_vector(self, tmp_path, monkeypatch):
        service, reviewed = _service(
            tmp_path, monkeypatch, vector=_vector(risk="medium"), fast_path=True, jev_chairman=True
        )
        result = await service.authorize(_request())

        assert result.status == "approved", result.error
        assert len(service._jev_client.calls) == 1  # one Jev call serves every consumer
        assert len(reviewed) == 1
        assert result.chairman.risk_level == "medium"
        assert result.chairman.threshold == "unanimous"
