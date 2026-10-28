# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Content assertions over the real attestation question catalog (v0.7.0).

These are pure file tests — no cluster, no network, no Jev. They guard the
catalog *contract* that the client, the router and the Helm chart all build
against:

  * `tier` splits the catalog into the triage (fast-path input) tier, the risk
    (chairman) tier and the attestation (council evidence) tier;
  * `polarity: safe` marks the one triage gate; everything else is adverse;
  * `noul` questions carry a two-key `criteria` mapping, because openjev's
    request shape takes one;
  * the chart ships a byte-identical copy of the same file.
"""

from pathlib import Path

import pytest
import yaml

from nomos.jev import load_catalog

REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = REPO_ROOT / "configs" / "attestation_questions.yaml"
CHART_CATALOG_PATH = (
    REPO_ROOT / "deploy" / "kubernetes" / "nomos" / "files" / "council" / CATALOG_PATH.name
)

VALID_TIERS = {"triage", "risk", "attestation"}

# The 44 questions as of catalog v0.4.0. They are frozen: the committed study in
# experiments/jev-council/ and the id assertions in tests/test_jev_attestation.py
# both reference them by id, so v0.5.0 is an ADDITIVE migration. This literal is
# spelled out rather than derived so that dropping or renaming one shows up here
# as a failing test instead of passing quietly.
ORIGINAL_ATTESTATION_IDS = frozenset(
    {
        "malformed_request",
        "description_matches_command",
        "underspecified_request",
        "internal_contradiction",
        "rollback_adequate",
        "scope_too_broad",
        "blast_radius_exceeds_justification",
        "wildcard_or_mass_operation",
        "affects_shared_or_critical_infra",
        "touches_production",
        "bypasses_staging",
        "irreversible_operation",
        "deletes_or_overwrites_data",
        "irrational_request",
        "justification_consistent",
        "justification_weak_or_boilerplate",
        "prompt_injection",
        "manipulates_council",
        "embedded_hidden_instructions",
        "claims_false_authorization",
        "social_engineering",
        "manufactured_urgency",
        "pressure_to_skip_review",
        "credential_exfiltration",
        "reads_sensitive_paths",
        "external_data_egress",
        "bulk_data_export",
        "tampers_with_audit",
        "disables_security_controls",
        "deletes_logs_or_history",
        "privilege_escalation",
        "modifies_rbac_or_permissions",
        "identity_spoofing",
        "establishes_persistence",
        "untrusted_code_or_source",
        "modifies_cicd_or_pipeline",
        "resource_exhaustion_or_dos",
        "skips_validation_or_testing",
        "touches_regulated_data",
        "overall_risk",
        "scope_breadth",
        "reversibility",
        "request_clarity",
        "justification_strength",
    }
)


@pytest.fixture(scope="module")
def catalog() -> dict:
    """The catalog as PyYAML sees it.

    The criteria tests below have to read the RAW parsed document, not the
    client's dataclasses: the trap they guard (`true:` parsing as the boolean
    True rather than the string "true") lives in the YAML layer and is invisible
    once something has already normalized the keys.
    """
    return yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def questions(catalog: dict) -> list[dict]:
    return catalog["questions"]


def test_catalog_declares_version_0_7_0(catalog: dict):
    # The catalog contract pins the field name `version`; the loader also reads
    # `catalog_version`. Both are emitted until they are collapsed to one, so
    # assert they exist AND agree — a silent divergence would mean the recorded
    # vector cites a different catalog than the questions asked.
    assert catalog["version"] == "0.7.0"
    assert catalog["catalog_version"] == "0.7.0"


def test_every_question_declares_a_valid_tier(questions: list[dict]):
    # `tier` is required from v0.5.0: the router reads the triage and risk tiers,
    # so a question with no tier would belong to no path and be answered for
    # nothing.
    missing = [q["id"] for q in questions if "tier" not in q]
    assert not missing, f"questions with no tier: {missing}"
    invalid = [(q["id"], q["tier"]) for q in questions if q["tier"] not in VALID_TIERS]
    assert not invalid, f"questions with an unknown tier: {invalid}"


def test_every_tier_is_populated(questions: list[dict]):
    # An empty triage tier would mean the fast path has nothing to read; an
    # empty risk tier would leave the Jev chairman without a risk level; an
    # empty attestation tier would mean the council gets no evidence.
    by_tier: dict[str, list[str]] = {tier: [] for tier in VALID_TIERS}
    for question in questions:
        by_tier[question["tier"]].append(question["id"])
    assert by_tier["triage"], "the triage tier is empty"
    assert by_tier["risk"] == ["risk_level"]
    assert by_tier["attestation"], "the attestation tier is empty"


def test_triage_tier_opens_with_the_read_only_eligibility_gate(questions: list[dict]):
    # The triage tier is one eligibility GATE plus N danger WEIGHTS, not a set of
    # independent flags. `read_only_operation` is the gate and the only inverted
    # question in the catalog (high = read-only = eligible); everything after it
    # is adverse-framed and composes into one weighted "is this read dangerous?"
    # evaluation. Losing or reordering the gate would leave a weighted sum with
    # nothing deciding eligibility — and an inverted term in it if the gate were
    # mistaken for a weight.
    triage_ids = [q["id"] for q in questions if q["tier"] == "triage"]
    assert triage_ids[0] == "read_only_operation"
    assert len(triage_ids) > 1, "the triage tier has a gate but no danger weights"
    gate = next(q for q in questions if q["id"] == "read_only_operation")
    assert gate["type"] == "noul"
    # The router finds the gate by polarity, so the gate must be the only
    # question that declares `safe`.
    assert [q["id"] for q in questions if q.get("polarity") == "safe"] == ["read_only_operation"]


def test_attestation_tier_still_contains_every_original_question(questions: list[dict]):
    # v0.5.0 is an additive migration. Anything that changes this set breaks the
    # committed jev-council study and test_jev_attestation.py's id assertions, so
    # it has to be a deliberate edit to this literal rather than a quiet drop.
    attestation_ids = {q["id"] for q in questions if q["tier"] == "attestation"}
    assert attestation_ids == set(ORIGINAL_ATTESTATION_IDS) | EVIDENCE_ATTESTATION_IDS


# v0.7.0 adds three questions that judge the request's evidence against it.
EVIDENCE_ATTESTATION_IDS = frozenset(
    {
        "certified_evidence_absent",
        "safety_claims_uncertified",
        "certified_evidence_contradicts_request",
    }
)


def test_question_ids_are_unique_across_both_tiers(questions: list[dict]):
    # Jev keys its request and response by question id, so a duplicate id across
    # tiers would collapse two questions into one answer rather than erroring.
    ids = [q["id"] for q in questions]
    duplicates = sorted({qid for qid in ids if ids.count(qid) > 1})
    assert not duplicates, f"duplicate question ids: {duplicates}"


def test_noul_criteria_are_exactly_the_two_string_keys(questions: list[dict]):
    # openjev takes `criteria` on noul as a two-key mapping. Unquoted `true:` /
    # `false:` in YAML are booleans, so the mapping would be keyed on True/False
    # and the serialized request would carry JSON keys the backend does not
    # recognise. Assert the key TYPE, not just the rendered name.
    for question in questions:
        if question["type"] != "noul":
            continue
        qid = question["id"]
        criteria = question.get("criteria")
        assert isinstance(criteria, dict), f"{qid}: noul criteria must be a mapping"
        keys = set(criteria)
        assert keys == {"true", "false"}, f"{qid}: unexpected criteria keys {keys}"
        for key, value in criteria.items():
            assert type(key) is str, f"{qid}: criteria key {key!r} is {type(key).__name__}, not str"
            assert isinstance(value, str), f"{qid}: criteria[{key!r}] must be a string"
            assert value.strip(), f"{qid}: criteria[{key!r}] is empty"


def test_score_criteria_are_non_empty_ordered_lists(questions: list[dict]):
    # The client normalizes a score answer by its level index over len(criteria),
    # so the list is both the legend and the divisor: an empty or unordered
    # mapping would make the normalized value meaningless.
    score_questions = [q for q in questions if q["type"] == "score"]
    assert score_questions, "the catalog has no score questions"
    for question in score_questions:
        criteria = question.get("criteria")
        assert isinstance(criteria, list), f"{question['id']}: score criteria must be a list"
        assert criteria, f"{question['id']}: score criteria is empty"
        assert all(isinstance(level, str) and level.strip() for level in criteria), (
            f"{question['id']}: score criteria levels must be non-empty strings"
        )


def test_choice_criteria_are_non_empty_mappings(questions: list[dict]):
    # `risk_level` is the first choice question; this guards the shape Jev
    # requires on it and any later one.
    assert any(q["type"] == "choice" for q in questions)
    for question in questions:
        if question["type"] != "choice":
            continue
        criteria = question.get("criteria")
        assert isinstance(criteria, dict), f"{question['id']}: choice criteria must be a mapping"
        assert criteria, f"{question['id']}: choice criteria is empty"


def test_chart_copy_is_identical_to_the_configs_copy():
    # The chart ships the same catalog in a ConfigMap. If the two drift, the
    # cluster asks a different question set than local runs and tests do, and
    # the recorded catalog_version stops identifying what was actually asked.
    assert CHART_CATALOG_PATH.read_bytes() == CATALOG_PATH.read_bytes()


def test_catalog_loads_through_the_jev_client_loader():
    # The content above is only useful if the shipped loader accepts it: it
    # validates `type` against the QuestionType enum and rejects duplicate or
    # empty ids, so this is the end-to-end check that the file is loadable.
    loaded = load_catalog(CATALOG_PATH)
    assert loaded.version == "0.7.0"
    raw = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))
    assert len(loaded.questions) == len(raw["questions"])
    assert {q.id for q in loaded.questions} == {q["id"] for q in raw["questions"]}
