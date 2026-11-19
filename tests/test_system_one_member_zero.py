# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
System one as member zero: the chairman, the read-only fast path, and the council prompt.

System one itself is stubbed: these tests hand the service a ready-made attestation
vector. The invariants under test are that every relaxation (quorum, fast path)
needs ALL of its conditions, and that any missing or malformed answer falls to
the safe outcome.
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from nomos.auth.authenticator import AgentIdentity
from nomos.auth.signing_executor import SigningCapacityError
from nomos.auth.writ_factory import WritFactory, WritSigningExpiredError
from nomos.config.settings import (
    SYSTEM_ONE_FAST_PATH_LEVELS,
    ChairmanConfig,
    CouncilSettings,
    OPAConfig,
    SystemOneChairmanConfig,
    SystemOneConfig,
    SystemOneFastPathConfig,
    SystemOneMutationFastPathConfig,
    validate_council_deployment_preflight,
)
from nomos.level_0.types import Attestation, AttestationVector, CouncilMember, Intent
from nomos.level_1.ledger import LedgerEntryType
from nomos.level_1.policy import DecisionType, PolicyRule
from nomos.llm.council_agent import IntentEvaluation, _format_intent_for_review
from nomos.nat.functions import AuthorizeInput, CouncilService
from nomos.opa import OPAEvaluationError, OPAPolicyDecision
from nomos.system_one import (
    SystemOneChairmanAgent,
    SystemOneClient,
    SystemOneEvaluationError,
    SystemOneRoutingError,
    chairman_decision,
    evaluate_fast_path,
    evaluate_mutation_fast_path,
)

# The external provider has no built-in endpoint or model; tests name both.
_EXTERNAL: dict[str, str] = {
    "provider": "external",
    "base_url": "https://system-one.internal.example",
    "model_version": "example-model-1.2.0",
}


def _vector(
    *,
    read_only: float = 0.99,
    danger: float = 0.02,
    risk: str = "low",
    confidence: float = 0.97,
    confidence_source: str = "reported",
) -> AttestationVector:
    return AttestationVector(
        model_version="example-model-1.2.0",
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
        decision = chairman_decision(_vector(), SystemOneChairmanConfig())
        assert decision.threshold == "quorum"
        assert decision.risk_level == "low"
        assert "example-model-1.2.0" in decision.reasoning

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
        decision = chairman_decision(vector, SystemOneChairmanConfig())
        assert decision.threshold == "unanimous"
        assert decision.risk_level == vector.by_id("risk_level").value

    @pytest.mark.parametrize("risk", ["low", "medium"])
    def test_unsure_low_or_medium_is_reported_high(self, risk):
        """A backend once answered 'low' at confidence 0.26 for an rm -rf in production."""
        decision = chairman_decision(_vector(risk=risk, confidence=0.26), SystemOneChairmanConfig())
        assert decision.risk_level == "high"
        assert decision.threshold == "unanimous"
        assert "low confidence" in decision.reasoning

    def test_unsure_critical_stays_critical(self):
        decision = chairman_decision(
            _vector(risk="critical", confidence=0.3), SystemOneChairmanConfig()
        )
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
        with pytest.raises(SystemOneRoutingError):
            chairman_decision(vector, SystemOneChairmanConfig())


class TestFastPathRouter:
    def test_all_conditions_met_is_eligible(self):
        result = evaluate_fast_path(_vector(), SystemOneFastPathConfig(enabled=True), **_ALL_CLEAR)
        assert result.eligible and result.approved
        assert result.failed == []
        assert result.values["read_only_operation"] == pytest.approx(0.99)
        assert result.values["risk_level"] == {"choice": "low", "confidence": 0.97}

    def test_disabled_still_records_a_shadow_verdict(self):
        result = evaluate_fast_path(_vector(), SystemOneFastPathConfig(), **_ALL_CLEAR)
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
        result = evaluate_fast_path(_vector(), SystemOneFastPathConfig(enabled=True), **conditions)
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
            (None, "no system one vector"),
        ],
    )
    def test_each_system_one_threshold_blocks(self, vector, reason):
        result = evaluate_fast_path(vector, SystemOneFastPathConfig(enabled=True), **_ALL_CLEAR)
        assert result.approved is False
        assert any(reason in failure for failure in result.failed), result.failed

    def test_question_max_danger_overrides_one_question(self):
        config = SystemOneFastPathConfig(
            enabled=True, question_max_danger={"discloses_sensitive_content": 0.30}
        )
        assert evaluate_fast_path(_vector(danger=0.29), config, **_ALL_CLEAR).approved is True
        result = evaluate_fast_path(_vector(danger=0.31), config, **_ALL_CLEAR)
        failure = "discloses_sensitive_content 0.31 > 0.30"
        assert any(failure in f for f in result.failed), result.failed
        assert result.values["question_max_danger"] == {"discloses_sensitive_content": 0.30}

    @pytest.mark.parametrize("question_id", ["read_danger", "prompt_injection"])
    def test_a_question_limit_with_no_triage_answer_blocks(self, question_id):
        """A configured limit the vector cannot be held to is a failure, not a pass.

        ``read_danger`` is not in this vector at all; ``prompt_injection`` is
        answered, but not as a triage weight, so its limit is never applied.
        """
        config = SystemOneFastPathConfig(enabled=True, question_max_danger={question_id: 0.05})
        result = evaluate_fast_path(_vector(), config, **_ALL_CLEAR)
        assert result.approved is False
        assert result.failed == [f"{question_id} has a limit but no triage answer"]

    @pytest.mark.parametrize("value", [None, ""])
    def test_an_unset_threshold_reference_keeps_the_declared_default(self, monkeypatch, value):
        # It used to resolve to 0.0, and a zero minimum passes every answer:
        # an unsure "low" at confidence 0.20 got a writ without the council.
        for name in (
            "SYSTEM_ONE_TEST_READ_ONLY",
            "SYSTEM_ONE_TEST_DANGER",
            "SYSTEM_ONE_TEST_CONFIDENCE",
        ):
            monkeypatch.delenv(name, raising=False)
            if value is not None:
                monkeypatch.setenv(name, value)
        config = SystemOneFastPathConfig(
            enabled=True,
            min_read_only="${SYSTEM_ONE_TEST_READ_ONLY}",
            max_danger="${SYSTEM_ONE_TEST_DANGER}",
            min_risk_confidence="${SYSTEM_ONE_TEST_CONFIDENCE}",
        )
        assert (config.min_read_only, config.max_danger, config.min_risk_confidence) == (
            0.95,
            0.20,
            0.90,
        )
        result = evaluate_fast_path(_vector(confidence=0.20), config, **_ALL_CLEAR)
        assert result.approved is False
        assert "risk_level confidence 0.20 < 0.90" in result.failed

    def test_a_set_threshold_reference_is_used(self, monkeypatch):
        monkeypatch.setenv("SYSTEM_ONE_TEST_CONFIDENCE", "0.75")
        config = SystemOneFastPathConfig(min_risk_confidence="${SYSTEM_ONE_TEST_CONFIDENCE}")
        assert config.min_risk_confidence == 0.75


class TestSystemOneChairmanAgent:
    def _chairman(self, *, allow_quorum: bool = True) -> SystemOneChairmanAgent:
        return SystemOneChairmanAgent(
            SystemOneChairmanConfig(),
            model_version="example-model-1.2.0",
            allow_quorum=allow_quorum,
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
    async def test_deterministic_floor_beats_a_confident_system_one(self):
        """Self-advocating prose says read-only; the command redirects to disk."""
        intent = _make_intent(
            "/bin/cat /etc/hostname > /workspace/out.txt",
            action_type="file.read",
            blast_radius="read-only, no state change",
        )
        decision = await self._chairman().decide_threshold(_with_vector(intent, _vector()))
        assert decision.threshold == "unanimous"
        assert decision.risk_level == "high"
        assert "system one assessment" in decision.reasoning

    @pytest.mark.asyncio
    async def test_deterministic_floor_raises_risk_when_already_unanimous(self):
        """The floor's risk level applies even when system one already chose unanimous."""
        intent = _make_intent(
            "/bin/cat /etc/hostname > /workspace/out.txt",
            action_type="file.read",
            blast_radius="read-only, no state change",
        )
        # Low risk, but the read-only gate is under the quorum minimum: unanimous / low.
        vector = _vector(read_only=0.5)
        assert chairman_decision(vector, SystemOneChairmanConfig()).risk_level == "low"
        decision = await self._chairman().decide_threshold(_with_vector(intent, vector))
        assert decision.threshold == "unanimous"
        assert decision.risk_level == "high"
        assert "system one assessment" in decision.reasoning

    @pytest.mark.asyncio
    async def test_deterministic_floor_keeps_a_stricter_system_one_risk(self):
        intent = _make_intent(
            "/bin/cat /etc/hostname > /workspace/out.txt",
            action_type="file.read",
            blast_radius="read-only, no state change",
        )
        decision = await self._chairman().decide_threshold(
            _with_vector(intent, _vector(risk="critical"))
        )
        assert decision.threshold == "unanimous"
        assert decision.risk_level == "critical"

    @pytest.mark.asyncio
    async def test_missing_permission_short_circuits(self):
        result = await self._chairman().pre_authorize(
            _with_vector(_make_intent(), _vector()), {"file.read"}, {"system.admin"}
        )
        assert result.authorized is False

    def test_provider_name_carries_pinned_version(self):
        assert self._chairman().provider_name == "system_one:example-model-1.2.0"


class TestConfig:
    def test_defaults_are_off(self):
        assert SystemOneConfig().enabled is False
        assert SystemOneConfig().fast_path.enabled is False
        assert ChairmanConfig().provider == "llm"

    def test_system_one_chairman_requires_system_one_enabled(self):
        with pytest.raises(ValidationError, match=r"requires system_one\.enabled"):
            CouncilSettings(chairman=ChairmanConfig(enabled=True, provider="system_one"))

    def test_unknown_chairman_provider_rejected(self):
        with pytest.raises(ValidationError, match="provider"):
            ChairmanConfig(provider="gpt")

    @pytest.mark.parametrize("alias", ["example-model-latest", "example-model-preview"])
    def test_floating_model_alias_rejected(self, alias):
        with pytest.raises(ValidationError, match="concrete version"):
            SystemOneConfig(**{**_EXTERNAL, "model_version": alias}, api_key="k")

    @pytest.mark.parametrize(
        ("provider", "variable"),
        [("external", "SYSTEM_ONE_API_KEY"), ("nemotron", "NVIDIA_API_KEY")],
    )
    def test_alpha_preflight_requires_the_key_when_system_one_is_enabled(
        self, monkeypatch, provider, variable
    ):
        # Without it every system one call fails and each intent quietly loses its
        # vector; the council would start with nothing to say so.
        monkeypatch.delenv("SYSTEM_ONE_API_KEY", raising=False)
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)

        def settings(**system_one: Any) -> CouncilSettings:
            return CouncilSettings(
                members=[],
                redis={
                    "password": "test-redis-password",  # pragma: allowlist secret
                    "account_integrity_key": "test-integrity-key",  # pragma: allowlist secret
                },
                nat={"host": "127.0.0.1"},
                system_one=SystemOneConfig(
                    **(_EXTERNAL if provider == "external" else {"provider": provider}),
                    **system_one,
                ),
            )

        with pytest.raises(ValueError, match=rf"system_one\.api_key \({variable}\) is required"):
            validate_council_deployment_preflight(settings(enabled=True), profile="alpha")
        keyed = settings(enabled=True, api_key="test-key")  # pragma: allowlist secret
        validate_council_deployment_preflight(keyed, profile="alpha")
        validate_council_deployment_preflight(settings(enabled=False), profile="alpha")


class TestCouncilPrompt:
    def test_full_vector_rendered_with_untrusted_framing(self):
        prompt = _format_intent_for_review(_with_vector(_make_intent(), _vector()))
        assert "## Pre-Council Attestations (system one)" in prompt
        assert "agent's own wording can move these numbers" in prompt
        assert "read_only_operation [triage, safe, informational]: 0.99" in prompt
        assert "risk_level [risk, adverse, informational]: low, confidence 0.97" in prompt
        assert "prompt_injection [attestation, adverse, deny-candidate]: 0.01" in prompt

    def test_no_section_without_a_vector(self):
        assert "Pre-Council Attestations" not in _format_intent_for_review(_make_intent())

    def test_a_name_that_reads_as_its_opposite_is_spelled_out(self):
        vector = _vector()
        vector.attestations.append(
            Attestation(
                question_id="description_matches_command",
                category="quality",
                qtype="noul",
                value=0.92,
                confidence=0.84,
                tier="attestation",
                action="flag",
                detail={"confidence_source": "certainty_proxy"},
            )
        )
        prompt = _format_intent_for_review(_with_vector(_make_intent(), vector))

        assert "whatever the question's name suggests" in prompt
        assert (
            "description_matches_command (HIGH = the command does something other than, "
            "or more than, the description says) [attestation, adverse, flag]: 0.92"
        ) in prompt
        # A name that reads the right way round is left as it is.
        assert "prompt_injection [attestation, adverse, deny-candidate]: 0.01" in prompt

    def test_every_spelled_out_name_is_an_adverse_question_of_the_catalog(self):
        from nomos.llm.council_agent import _INVERTED_NAME_READINGS
        from nomos.system_one import load_catalog

        catalog = load_catalog(Path(__file__).parents[1] / "configs/attestation_questions.yaml")
        polarity = {question.id: question.polarity for question in catalog.questions}

        assert {polarity.get(name) for name in _INVERTED_NAME_READINGS} == {"adverse"}


def _mutation_vector(
    *, risk: str = "medium", reversibility: float = 0.05, broad_read: float = 0.05
) -> AttestationVector:
    """A local, reversible edit: read-only gate low, risk medium, adverse answers low."""
    vector = _vector(read_only=0.03, risk=risk)
    for question_id, value in (("read_danger", 0.1), ("broad_or_enumerating_read", broad_read)):
        vector.attestations.append(
            Attestation(
                question_id=question_id,
                category="risk",
                qtype="noul",
                value=value,
                confidence=abs(2 * value - 1),
                tier="triage",
                detail={"confidence_source": "certainty_proxy"},
            )
        )
    vector.attestations.append(
        Attestation(
            question_id="reversibility",
            category="destructive",
            qtype="score",
            value=reversibility,
            confidence=0.9,
            detail={"confidence_source": "reported"},
        )
    )
    return vector


_MUTATION_LIMITS = {"reversibility": 0.20, "prompt_injection": 0.20}


class TestMutationFastPathRouter:
    def test_all_conditions_met_is_eligible(self):
        config = SystemOneMutationFastPathConfig(enabled=True, question_limits=_MUTATION_LIMITS)
        result = evaluate_mutation_fast_path(
            _mutation_vector(), config, SystemOneFastPathConfig(), **_ALL_CLEAR
        )
        assert result.approved, result.failed
        assert result.values["reversibility"] == pytest.approx(0.05)
        assert result.values["risk_level"] == {"choice": "medium", "confidence": 0.97}

    def test_disabled_still_records_a_shadow_verdict(self):
        config = SystemOneMutationFastPathConfig(question_limits=_MUTATION_LIMITS)
        result = evaluate_mutation_fast_path(
            _mutation_vector(), config, SystemOneFastPathConfig(), **_ALL_CLEAR
        )
        assert result.eligible is True
        assert result.approved is False

    @pytest.mark.parametrize(
        ("vector", "limits", "reason"),
        [
            (_mutation_vector(reversibility=0.21), _MUTATION_LIMITS, "reversibility 0.21 > 0.20"),
            (_mutation_vector(risk="high"), _MUTATION_LIMITS, "risk_level is high"),
            (
                _mutation_vector(),
                {"irreversible_operation": 0.2},
                "irreversible_operation not answered",
            ),
            (_mutation_vector(), {}, "no question_limits configured"),
            (None, _MUTATION_LIMITS, "no system one vector"),
        ],
    )
    def test_each_system_one_condition_blocks(self, vector, limits, reason):
        config = SystemOneMutationFastPathConfig(question_limits=limits)
        result = evaluate_mutation_fast_path(
            vector, config, SystemOneFastPathConfig(), **_ALL_CLEAR
        )
        assert result.eligible is False
        assert any(reason in failure for failure in result.failed), result.failed

    def test_max_risk_level_low_refuses_medium(self):
        config = SystemOneMutationFastPathConfig(
            max_risk_level="low", question_limits=_MUTATION_LIMITS
        )
        result = evaluate_mutation_fast_path(
            _mutation_vector(), config, SystemOneFastPathConfig(), **_ALL_CLEAR
        )
        assert "risk_level is medium" in result.failed

    @pytest.mark.parametrize(
        "question_id", ["opaque_command_payload", "deletes_or_overwrites_data"]
    )
    @pytest.mark.parametrize(("value", "eligible"), [(0.05, True), (0.9, False)])
    def test_shipped_limit_sends_a_hidden_payload_or_data_loss_to_the_council(
        self, question_id, value, eligible
    ):
        shipped = yaml.safe_load((Path(__file__).parents[1] / "configs/council.yaml").read_text())
        limit = shipped["system_one"]["mutation_fast_path"]["question_limits"][question_id]
        vector = _mutation_vector()
        vector.attestations.append(
            Attestation(
                question_id=question_id,
                category="risk",
                qtype="noul",
                value=value,
                confidence=0.8,
                tier="triage",
                detail={"confidence_source": "certainty_proxy"},
            )
        )
        config = SystemOneMutationFastPathConfig(enabled=True, question_limits={question_id: limit})
        result = evaluate_mutation_fast_path(
            vector, config, SystemOneFastPathConfig(), **_ALL_CLEAR
        )

        assert result.eligible is eligible
        assert any(question_id in reason for reason in result.failed) is not eligible

    def test_unsure_risk_level_blocks(self):
        vector = _mutation_vector()
        risk = next(a for a in vector.attestations if a.question_id == "risk_level")
        vector.attestations[vector.attestations.index(risk)] = risk.model_copy(
            update={"confidence": 0.85}
        )
        config = SystemOneMutationFastPathConfig(question_limits=_MUTATION_LIMITS)
        result = evaluate_mutation_fast_path(
            vector, config, SystemOneFastPathConfig(), **_ALL_CLEAR
        )
        assert "risk_level confidence 0.85 < 0.90" in result.failed

    @pytest.mark.parametrize(
        "override", [{"authenticated": False}, {"policy_allows": False}, {"floor_fired": True}]
    )
    def test_deterministic_exclusions_block(self, override):
        config = SystemOneMutationFastPathConfig(enabled=True, question_limits=_MUTATION_LIMITS)
        result = evaluate_mutation_fast_path(
            _mutation_vector(), config, SystemOneFastPathConfig(), **{**_ALL_CLEAR, **override}
        )
        assert result.approved is False

    def test_read_danger_is_held_to_the_read_only_limit(self):
        # Regression (2026-10-28): `pip list`, refused by the read-only path on
        # broad_or_enumerating_read 0.32 > 0.30, was authorised as a mutation.
        config = SystemOneMutationFastPathConfig(question_limits=_MUTATION_LIMITS)
        read_only = SystemOneFastPathConfig(question_max_danger={"broad_or_enumerating_read": 0.30})
        vector = _mutation_vector(broad_read=0.32)
        result = evaluate_mutation_fast_path(vector, config, read_only, **_ALL_CLEAR)
        assert "broad_or_enumerating_read 0.32 > 0.30 (read-only limit)" in result.failed
        ok = evaluate_mutation_fast_path(
            _mutation_vector(broad_read=0.29), config, read_only, **_ALL_CLEAR
        )
        assert ok.eligible, ok.failed

    def test_enabled_without_limits_is_rejected(self):
        with pytest.raises(ValidationError, match="requires question_limits"):
            SystemOneMutationFastPathConfig(enabled=True)


_SYSTEM_ONE_BASE: dict[str, Any] = {
    **_EXTERNAL,
    "api_key": "k",
}  # pragma: allowlist secret


class TestSystemOneLevels:
    """The opt-in levels stack: prefilter < read-only fast path < mutation fast path < reserved."""

    @pytest.mark.parametrize(
        ("enabled", "read_only", "mutation"),
        [
            (False, False, False),  # system one off
            (True, False, False),  # level 1: prefilter, attestations, risk
            (True, True, False),  # level 2: + read-only writs
            (True, True, True),  # level 3: + low-risk mutation writs
        ],
    )
    def test_each_level_loads(self, enabled, read_only, mutation):
        config = SystemOneConfig(
            **_SYSTEM_ONE_BASE,
            enabled=enabled,
            fast_path=SystemOneFastPathConfig(enabled=read_only),
            mutation_fast_path=SystemOneMutationFastPathConfig(
                enabled=mutation, question_limits=_MUTATION_LIMITS
            ),
        )
        assert (config.enabled, config.fast_path.enabled) == (enabled, read_only)
        assert config.mutation_fast_path.enabled is mutation

    def test_read_only_fast_path_requires_system_one(self):
        with pytest.raises(
            ValidationError, match=r"fast_path\.enabled requires system_one\.enabled"
        ):
            SystemOneConfig(
                **_SYSTEM_ONE_BASE, enabled=False, fast_path=SystemOneFastPathConfig(enabled=True)
            )

    def test_mutation_fast_path_requires_read_only_fast_path(self):
        with pytest.raises(
            ValidationError,
            match=r"mutation_fast_path\.enabled requires system_one\.fast_path\.enabled",
        ):
            SystemOneConfig(
                **_SYSTEM_ONE_BASE,
                enabled=True,
                mutation_fast_path=SystemOneMutationFastPathConfig(
                    enabled=True, question_limits=_MUTATION_LIMITS
                ),
            )

    @pytest.mark.parametrize(
        "name", ["moderate_risk_fast_path", "high_risk_fast_path", "critical_risk_fast_path"]
    )
    def test_reserved_levels_cannot_be_enabled(self, name):
        levels = {
            "fast_path": SystemOneFastPathConfig(enabled=True),
            "mutation_fast_path": SystemOneMutationFastPathConfig(
                enabled=True, question_limits=_MUTATION_LIMITS
            ),
            name: {"enabled": True},
        }
        with pytest.raises(ValidationError, match="reserved and not implemented"):
            SystemOneConfig(**_SYSTEM_ONE_BASE, enabled=True, **levels)

    def test_evidence_question_limits_require_include_evidence(self):
        levels = {
            "fast_path": SystemOneFastPathConfig(enabled=True),
            "mutation_fast_path": SystemOneMutationFastPathConfig(
                enabled=True,
                question_limits={**_MUTATION_LIMITS, "certified_evidence_contradicts_request": 0.5},
            ),
        }
        with pytest.raises(ValidationError, match=r"asked only with system_one\.include_evidence"):
            SystemOneConfig(**_SYSTEM_ONE_BASE, enabled=True, **levels)
        config = SystemOneConfig(**_SYSTEM_ONE_BASE, enabled=True, include_evidence=True, **levels)
        assert config.include_evidence is True

    def test_final_refusal_requires_the_mutation_path(self):
        with pytest.raises(ValidationError, match="escalate_to_council false requires"):
            SystemOneConfig(
                **_SYSTEM_ONE_BASE,
                enabled=True,
                mutation_fast_path=SystemOneMutationFastPathConfig(escalate_to_council=False),
            )

    def test_defaults_are_all_off(self):
        config = SystemOneConfig()
        levels = [config.enabled] + [
            getattr(config, n).enabled for n in SYSTEM_ONE_FAST_PATH_LEVELS
        ]
        assert not any(levels)
        assert config.include_evidence is False
        assert config.mutation_fast_path.escalate_to_council is True


class _StubSystemOne:
    def __init__(self, vector: AttestationVector):
        self.vector = vector
        self.calls: list[Intent] = []

    async def evaluate(self, intent: Intent) -> AttestationVector:
        self.calls.append(intent)
        return self.vector


class _FailingSystemOne:
    async def evaluate(self, _intent: Intent) -> AttestationVector:
        raise SystemOneEvaluationError("system one returned HTTP 503")


class _AllowOPA:
    async def evaluate(self, _input_doc):
        return OPAPolicyDecision(allow=True)


class _DownOPA:
    async def evaluate(self, _input_doc):
        raise OPAEvaluationError("connection refused")


def _service(
    tmp_path,
    monkeypatch,
    *,
    vector: AttestationVector,
    fast_path: bool,
    system_one_chairman: bool = False,
    mutation_fast_path: bool = False,
    escalate_to_council: bool = True,
) -> tuple[CouncilService, list[Intent]]:
    chairman = (
        ChairmanConfig(enabled=True, provider="system_one", allow_quorum=True)
        if system_one_chairman
        else ChairmanConfig()
    )
    service = CouncilService(
        CouncilSettings(
            council_id="test",
            require_auth=False,
            members=[],
            opa=OPAConfig(enabled=True),
            system_one=SystemOneConfig(
                enabled=True,
                **_EXTERNAL,
                api_key="k",  # pragma: allowlist secret
                fast_path=SystemOneFastPathConfig(enabled=fast_path),
                mutation_fast_path=SystemOneMutationFastPathConfig(
                    enabled=mutation_fast_path,
                    question_limits=_MUTATION_LIMITS,
                    escalate_to_council=escalate_to_council,
                ),
            ),
            chairman=chairman,
            council_key_path=str(tmp_path / "keys"),
        )
    )
    service._system_one_client = _StubSystemOne(vector)
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
        assert payload["attestation"]["model_version"] == "example-model-1.2.0"

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
    @pytest.mark.parametrize("decision", [DecisionType.DENY, DecisionType.REQUIRE_QUORUM])
    async def test_configured_rule_named_default_blocks_the_fast_path(
        self, tmp_path, monkeypatch, decision
    ):
        """Only the fallback for "no rule matched" is exempt, not a rule with its id."""
        service, _reviewed = _service(tmp_path, monkeypatch, vector=_vector(), fast_path=True)
        service.council._policy_engine.add_rule(
            PolicyRule(rule_id="default", name="Catch-all", priority=5, decision=decision)
        )
        result = await service.authorize(_request())

        assert result.chairman is None or result.chairman.threshold != "fast_path"
        fast_path = _review_record(service)["attestation"]["fast_path"]
        assert fast_path["approved"] is False
        assert "council policy rule requires review" in fast_path["failed"]

    @pytest.mark.asyncio
    async def test_opa_outage_under_fail_open_goes_to_council(self, tmp_path, monkeypatch):
        """Fail-open sends the intent on to the council; OPA did not run, so no fast path."""
        service, reviewed = _service(tmp_path, monkeypatch, vector=_vector(), fast_path=True)
        monkeypatch.setattr(service._settings.opa, "fail_closed", False)
        service._opa_client = _DownOPA()
        result = await service.authorize(_request())

        assert result.status == "approved", result.error
        assert len(reviewed) == 1
        failed = _review_record(service)["attestation"]["fast_path"]["failed"]
        assert "OPA did not run and allow" in failed

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("error", "retryable"),
        [
            (SigningCapacityError("Writ signing temporarily at capacity; no writ issued"), True),
            (WritSigningExpiredError("Writ validity window closed before signing"), False),
        ],
    )
    async def test_only_a_full_signing_lane_is_retryable(
        self, tmp_path, monkeypatch, error, retryable
    ):
        service, _reviewed = _service(tmp_path, monkeypatch, vector=_vector(), fast_path=True)

        async def sign(_signer, _payload):
            raise error

        monkeypatch.setattr(service._writ_signing, "sign", sign)
        result = await service.authorize(_request())

        assert result.status == "error" and result.writ is None
        assert result.model_dump().get("retryable", False) is retryable

    @pytest.mark.asyncio
    async def test_low_read_only_score_goes_to_council(self, tmp_path, monkeypatch):
        service, reviewed = _service(
            tmp_path, monkeypatch, vector=_vector(read_only=0.88), fast_path=True
        )
        result = await service.authorize(_request())
        assert result.status == "approved", result.error
        assert len(reviewed) == 1

    @pytest.mark.asyncio
    async def test_system_one_chairman_reads_the_same_vector(self, tmp_path, monkeypatch):
        service, reviewed = _service(
            tmp_path,
            monkeypatch,
            vector=_vector(risk="medium"),
            fast_path=True,
            system_one_chairman=True,
        )
        result = await service.authorize(_request())

        assert result.status == "approved", result.error
        assert (
            len(service._system_one_client.calls) == 1
        )  # one system one call serves every consumer
        assert len(reviewed) == 1
        assert result.chairman.risk_level == "medium"
        assert result.chairman.threshold == "unanimous"

    @pytest.mark.asyncio
    async def test_system_one_failure_goes_to_council_without_a_vector(self, tmp_path, monkeypatch):
        service, reviewed = _service(tmp_path, monkeypatch, vector=_vector(), fast_path=True)
        service._system_one_client = _FailingSystemOne()
        result = await service.authorize(_request())

        assert result.status == "approved", result.error
        assert len(reviewed) == 1
        assert reviewed[0]._attestation is None
        assert WritFactory.deserialize_writ(result.writ).threshold_used != "fast_path"
        assert _review_record(service)["attestation"] == {}

    @pytest.mark.asyncio
    async def test_fast_path_is_refused_when_opa_did_not_run(self, tmp_path, monkeypatch):
        """OPA disabled: there is no decision, so nothing allowed the intent."""
        service, reviewed = _service(tmp_path, monkeypatch, vector=_vector(), fast_path=True)
        service._opa_client = None
        result = await service.authorize(_request())

        assert result.status == "approved", result.error
        assert len(reviewed) == 1
        assert WritFactory.deserialize_writ(result.writ).threshold_used != "fast_path"
        fast_path = _review_record(service)["attestation"]["fast_path"]
        assert fast_path["approved"] is False
        assert "OPA did not run and allow" in fast_path["failed"]

    @pytest.mark.asyncio
    async def test_oversize_state_goes_to_council_without_a_vector(
        self, tmp_path, monkeypatch, caplog
    ):
        """State over the cap is not sent to system one, so there is no vector to act on."""
        service, reviewed = _service(tmp_path, monkeypatch, vector=_vector(), fast_path=True)
        client = SystemOneClient(
            service._settings.system_one.model_copy(update={"max_state_chars": 256})
        )
        posted: list[dict] = []

        async def post(body):
            posted.append(body)
            return {}

        monkeypatch.setattr(client, "_post", post)
        service._system_one_client = client
        result = await service.authorize(_request())

        assert posted == []
        assert "over the 256 cap" in caplog.text
        assert result.status == "approved", result.error
        assert len(reviewed) == 1
        assert reviewed[0]._attestation is None
        assert WritFactory.deserialize_writ(result.writ).threshold_used != "fast_path"
        assert _review_record(service)["attestation"] == {}


class TestServiceMutationFastPath:
    @pytest.mark.asyncio
    async def test_low_risk_mutation_mints_without_the_council(self, tmp_path, monkeypatch):
        service, reviewed = _service(
            tmp_path,
            monkeypatch,
            vector=_mutation_vector(),
            fast_path=True,
            mutation_fast_path=True,
        )
        result = await service.authorize(_request(command="/usr/bin/mkdir /workspace/out"))

        assert result.status == "approved", result.error
        assert reviewed == []
        assert result.chairman.threshold == "fast_path_mutation"
        writ = WritFactory.deserialize_writ(result.writ)
        assert writ.threshold_used == "fast_path_mutation"
        assert writ.verify(service._active_signer().public_key_bytes())
        attestation = _review_record(service)["attestation"]
        assert attestation["fast_path"]["approved"] is False
        assert attestation["mutation_fast_path"]["approved"] is True

    @pytest.mark.asyncio
    async def test_disabled_mutation_path_goes_to_council_and_records_shadow(
        self, tmp_path, monkeypatch
    ):
        service, reviewed = _service(
            tmp_path, monkeypatch, vector=_mutation_vector(), fast_path=True
        )
        result = await service.authorize(_request(command="/usr/bin/mkdir /workspace/out"))

        assert result.status == "approved", result.error
        assert len(reviewed) == 1
        shadow = _review_record(service)["attestation"]["mutation_fast_path"]
        assert shadow == {**shadow, "enabled": False, "eligible": True, "approved": False}

    @pytest.mark.asyncio
    async def test_risky_mutation_goes_to_council(self, tmp_path, monkeypatch):
        service, reviewed = _service(
            tmp_path,
            monkeypatch,
            vector=_mutation_vector(reversibility=0.9),
            fast_path=True,
            mutation_fast_path=True,
        )
        result = await service.authorize(_request(command="/usr/bin/mkdir /workspace/out"))
        assert result.status == "approved", result.error
        assert len(reviewed) == 1
        failed = _review_record(service)["attestation"]["mutation_fast_path"]["failed"]
        assert "reversibility 0.90 > 0.20" in failed


class TestServiceEscalation:
    """escalate_to_council: on (default) a refused intent goes to the council; off it is final."""

    @pytest.mark.asyncio
    async def test_refusal_escalates_to_the_council_by_default(self, tmp_path, monkeypatch):
        service, reviewed = _service(
            tmp_path,
            monkeypatch,
            vector=_mutation_vector(reversibility=0.9),
            fast_path=True,
            mutation_fast_path=True,
        )
        result = await service.authorize(_request(command="/usr/bin/mkdir /workspace/out"))
        assert result.status == "approved", result.error
        assert len(reviewed) == 1

    @pytest.mark.asyncio
    async def test_refusal_is_final_when_escalation_is_off(self, tmp_path, monkeypatch):
        service, reviewed = _service(
            tmp_path,
            monkeypatch,
            vector=_mutation_vector(reversibility=0.9),
            fast_path=True,
            mutation_fast_path=True,
            escalate_to_council=False,
        )
        result = await service.authorize(_request(command="/usr/bin/mkdir /workspace/out"))
        assert result.status == "denied"
        assert reviewed == []
        assert result.chairman.threshold == "system_one_refusal"
        assert "reversibility 0.90 > 0.20" in result.error
        payload = _review_record(service)
        assert payload["final"]["decision"] == "deny"
        assert payload["attestation"]["mutation_fast_path"]["council_required"] is False

    @pytest.mark.asyncio
    async def test_policy_rule_still_reaches_the_council_when_escalation_is_off(
        self, tmp_path, monkeypatch
    ):
        """A named review rule is not system one's to overrule: the council still convenes."""
        service, reviewed = _service(
            tmp_path,
            monkeypatch,
            vector=_mutation_vector(reversibility=0.9),
            fast_path=True,
            mutation_fast_path=True,
            escalate_to_council=False,
        )
        await service.authorize(
            _request(command="/usr/bin/mkdir /workspace/out", target_resource="prod/payments")
        )
        assert len(reviewed) == 1
        assert _review_record(service)["attestation"]["mutation_fast_path"]["council_required"]

    @pytest.mark.asyncio
    async def test_low_risk_mutation_still_mints_when_escalation_is_off(
        self, tmp_path, monkeypatch
    ):
        service, reviewed = _service(
            tmp_path,
            monkeypatch,
            vector=_mutation_vector(),
            fast_path=True,
            mutation_fast_path=True,
            escalate_to_council=False,
        )
        result = await service.authorize(_request(command="/usr/bin/mkdir /workspace/out"))
        assert result.status == "approved", result.error
        assert result.chairman.threshold == "fast_path_mutation"
        assert reviewed == []
