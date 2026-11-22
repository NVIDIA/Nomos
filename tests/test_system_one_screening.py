# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Policy screening defaults, strict routing and exact-operation replay selection."""

from dataclasses import FrozenInstanceError

import pytest
from pydantic import ValidationError

from nomos.config.settings import (
    SystemOneConfig,
    SystemOneScreeningConfig,
    SystemOneScreeningRule,
)
from nomos.level_0.types import Attestation, AttestationVector
from nomos.level_0.types.attestation import QuestionType
from nomos.system_one.client import AttestationCatalog, CatalogQuestion
from nomos.system_one.screening import (
    evaluate_screening,
    selected_screening_rules,
    validate_screening_catalog,
)


def _config(*, mode="enforce", default_action="deny", **rules):
    return SystemOneScreeningConfig(
        enabled=True, mode=mode, default_action=default_action, rules=rules
    )


def _answer(question_id="disclosure", value=0.9, **fields):
    # Deliberately bypass coercion to exercise the routing boundary's checks of
    # malformed vectors as well as normal parsed answers.
    return Attestation.model_construct(
        question_id=question_id,
        category="policy",
        qtype=fields.pop("qtype", "noul"),
        polarity=fields.pop("polarity", "adverse"),
        value=value,
        confidence=1.0,
        **fields,
    )


def _vector(*answers):
    return AttestationVector.model_construct(
        model_version="fixture-model-v1",
        catalog_version="fixture-catalog-v1",
        attestations=list(answers),
    )


def _catalog(*questions):
    return AttestationCatalog(version="fixture-v1", questions=list(questions))


def _question(question_id="disclosure", *, qtype=QuestionType.NOUL, polarity="adverse"):
    return CatalogQuestion(id=question_id, category="policy", qtype=qtype, polarity=polarity)


def test_screening_is_off_and_shadow_by_default_but_action_defaults_to_deny():
    config = SystemOneScreeningConfig()
    assert config.enabled is False
    assert config.mode == "shadow"
    assert config.default_action == "deny"
    assert config.rules == {}
    assert evaluate_screening(None, config).decision == "continue"
    assert selected_screening_rules(config) == {}


def test_omitted_action_inherits_deny_only_when_rule_matches():
    config = _config(disclosure={"threshold": 0.8})
    assert config.rules["disclosure"].action is None
    assert evaluate_screening(_vector(_answer(value=0.79)), config).decision == "continue"
    assert evaluate_screening(_vector(_answer(value=0.8)), config).decision == "denied"
    assert config.rules["disclosure"].action is None


def test_configured_default_and_explicit_override_are_independent():
    config = _config(
        default_action="requires_human_approval",
        install={"threshold": 0.8},
        disclosure={"threshold": 0.8, "action": "deny"},
    )
    selected = selected_screening_rules(config)
    assert selected["install"].action == "requires_human_approval"
    assert selected["disclosure"].action == "deny"
    result = evaluate_screening(_vector(_answer("install"), _answer(value=0.1)), config)
    assert result.decision == "requires_human_approval"
    assert result.enforced_decision == "requires_human_approval"


def test_deny_wins_over_human_review_regardless_of_rule_order():
    for rules in (
        {
            "install": {"threshold": 0.8, "action": "requires_human_approval"},
            "disclosure": {"threshold": 0.8},
        },
        {
            "disclosure": {"threshold": 0.8},
            "install": {"threshold": 0.8, "action": "requires_human_approval"},
        },
    ):
        result = evaluate_screening(_vector(_answer("install"), _answer()), _config(**rules))
        assert result.decision == "denied"
        assert {item.question_id for item in result.matched} == {"install", "disclosure"}


@pytest.mark.parametrize("value,expected", [(0.1, "continue"), (0.9, "denied")])
def test_shadow_retains_the_observed_verdict_without_enforcing_it(value, expected):
    result = evaluate_screening(
        _vector(_answer(value=value)), _config(mode="shadow", disclosure={"threshold": 0.8})
    )
    assert result.decision == expected
    assert result.enforced_decision == "continue"


def test_shadow_errors_do_not_change_authorization():
    result = evaluate_screening(None, _config(mode="shadow", disclosure={"threshold": 0.8}))
    assert result.decision == "error"
    assert result.enforced_decision == "continue"


def test_policy_only_selects_effective_deny_rules_and_ignores_ask_answers():
    config = _config(
        install={"threshold": 0.8, "action": "requires_human_approval"},
        disclosure={"threshold": 0.8},
        outside={"threshold": 0.8, "action": "requires_human_approval"},
    )
    assert set(selected_screening_rules(config, policy_only=True)) == {"disclosure"}
    assert set(selected_screening_rules(config)) == {"install", "disclosure", "outside"}
    result = evaluate_screening(_vector(_answer(value=0.1)), config, policy_only=True)
    assert result.decision == "continue"
    assert [item.question_id for item in result.evaluated] == ["disclosure"]
    assert evaluate_screening(_vector(_answer()), config, policy_only=True).decision == "denied"
    assert evaluate_screening(None, config, policy_only=True).decision == "error"


def test_policy_only_without_deny_rules_needs_no_model_vector():
    config = _config(default_action="requires_human_approval", install={"threshold": 0.8})
    assert selected_screening_rules(config, policy_only=True) == {}
    assert evaluate_screening(None, config, policy_only=True).decision == "continue"


def test_disabled_rules_do_not_require_answers_or_catalog_validation():
    config = SystemOneScreeningConfig(rules={"missing": {"threshold": 0.8}})
    validate_screening_catalog(_catalog(), config)
    assert evaluate_screening(None, config).decision == "continue"


def test_unselected_action_metadata_does_not_become_policy():
    config = _config(disclosure={"threshold": 0.8})
    result = evaluate_screening(
        _vector(_answer(value=0.1), _answer("unselected", action="deny-candidate")), config
    )
    assert result.decision == "continue"
    assert len(result.evaluated) == 1


@pytest.mark.parametrize("value", [True, False, "0.9", None, float("nan"), float("inf"), -0.1, 1.1])
def test_invalid_answer_probability_cannot_match_or_clear_a_rule(value):
    result = evaluate_screening(
        _vector(_answer(value=value)), _config(disclosure={"threshold": 0.8})
    )
    assert result.decision == "error"
    assert result.enforced_decision == "error"
    assert result.error == "Screening contains an invalid answer."
    assert result.matched == ()


@pytest.mark.parametrize("fields", [{"qtype": "choice"}, {"qtype": "score"}, {"polarity": "safe"}])
def test_wrong_answer_semantics_are_errors(fields):
    result = evaluate_screening(_vector(_answer(**fields)), _config(disclosure={"threshold": 0.8}))
    assert result.decision == "error"


@pytest.mark.parametrize("vector", [None, _vector(), _vector(_answer(), _answer())])
def test_missing_or_duplicate_answers_fail_closed(vector):
    result = evaluate_screening(vector, _config(disclosure={"threshold": 0.8}))
    assert result.decision == "error"
    assert result.error


def test_missing_answer_does_not_silently_clear_another_required_rule():
    config = _config(disclosure={"threshold": 0.8}, install={"threshold": 0.8})
    result = evaluate_screening(_vector(_answer()), config)
    assert result.decision == "error"
    assert [item.question_id for item in result.evaluated] == ["disclosure"]


def test_result_is_immutable_and_audit_serialization_copies_values():
    result = evaluate_screening(_vector(_answer()), _config(disclosure={"threshold": 0.8}))
    assert result.to_dict() == {
        "decision": "denied",
        "mode": "enforce",
        "enforced_decision": "denied",
        "evaluated": [
            {
                "question_id": "disclosure",
                "threshold": 0.8,
                "probability": 0.9,
                "action": "deny",
                "matched": True,
            }
        ],
        "matched": ["disclosure"],
        "error": "",
    }
    with pytest.raises(FrozenInstanceError):
        result.decision = "continue"
    with pytest.raises(FrozenInstanceError):
        result.evaluated[0].threshold = 1.0
    serialized = result.to_dict()
    serialized["evaluated"][0]["probability"] = 0
    assert result.evaluated[0].probability == 0.9


def test_validate_catalog_accepts_only_explicit_selected_questions():
    config = _config(disclosure={"threshold": 0.8})
    validate_screening_catalog(
        _catalog(_question(), _question("risk", qtype=QuestionType.CHOICE)), config
    )


@pytest.mark.parametrize(
    "question",
    [
        _question("other"),
        _question(qtype=QuestionType.SCORE),
        _question(qtype=QuestionType.CHOICE),
        _question(polarity="safe"),
    ],
)
def test_invalid_catalog_rule_is_a_startup_error(question):
    with pytest.raises(ValueError, match="Screening question"):
        validate_screening_catalog(_catalog(question), _config(disclosure={"threshold": 0.8}))


@pytest.mark.parametrize(
    "threshold", [True, False, "0.8", None, float("nan"), float("inf"), -0.1, 1.1]
)
def test_threshold_is_an_explicit_finite_numeric_probability(threshold):
    with pytest.raises(ValidationError):
        SystemOneScreeningRule(threshold=threshold)


@pytest.mark.parametrize("threshold", [0, 0.8, 1])
def test_threshold_boundaries_are_valid(threshold):
    assert SystemOneScreeningRule(threshold=threshold).threshold == threshold


def test_missing_threshold_is_rejected():
    with pytest.raises(ValidationError):
        SystemOneScreeningRule()


@pytest.mark.parametrize(
    "data",
    [
        {"enabled": True},
        {"default_action": "allow"},
        {"mode": "enabled"},
        {"unknown": True},
        {"rules": {"question": {"threshold": 0.8, "action": "allow"}}},
        {"rules": {"question": {"threshold": 0.8, "unknown": True}}},
        {"rules": {"": {"threshold": 0.8}}},
        {"rules": {"leading space": {"threshold": 0.8}}},
        {"rules": {"é": {"threshold": 0.8}}},
        {"rules": {"a" * 129: {"threshold": 0.8}}},
    ],
)
def test_invalid_screening_configuration_is_rejected(data):
    with pytest.raises(ValidationError):
        SystemOneScreeningConfig(**data)


def test_screening_requires_system_one_to_be_enabled():
    with pytest.raises(ValidationError, match=r"screening\.enabled requires system_one\.enabled"):
        SystemOneConfig(screening={"enabled": True, "rules": {"q": {"threshold": 0.8}}})
    config = SystemOneConfig(
        enabled=True, screening={"enabled": True, "rules": {"q": {"threshold": 0.8}}}
    )
    assert config.screening.default_action == "deny"


def test_shadow_retains_legacy_vector_but_observes_invalid_raw_answer():
    answer = _answer(value=0.9, detail={"screening_answer_valid": False})
    result = evaluate_screening(
        _vector(answer), _config(mode="shadow", disclosure={"threshold": 0.8})
    )
    assert result.decision == "error"
    assert result.enforced_decision == "continue"
    assert result.error == "Screening contains an invalid answer."
    assert answer.value == 0.9
    assert answer.detail == {"screening_answer_valid": False}
