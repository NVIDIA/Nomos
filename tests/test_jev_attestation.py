# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from nomos.config.settings import JEV_EVIDENCE_QUESTIONS, JevConfig
from nomos.jev import (
    AttestationCatalog,
    JevClient,
    JevEvaluationError,
    build_request,
    build_state,
    load_catalog,
    parse_response,
    redact,
)
from nomos.jev.client import CONFIDENCE_CERTAINTY_PROXY, CONFIDENCE_REPORTED
from nomos.level_0.types import AttestationVector, Intent

CATALOG_PATH = Path(__file__).resolve().parents[1] / "configs" / "attestation_questions.yaml"

# A three-question catalog in the catalog-contract format, one question per type.
# Used instead of the shipped catalog wherever a test is about how the CLIENT
# adapts to a backend: those assertions must not move when the catalog's
# content does. The ids mirror the openjev request contract's own example.
_FIXTURE_CATALOG_YAML = """
version: "0.5.0-fixture"
questions:
  - id: dog_present
    tier: triage
    polarity: safe
    category: risk
    type: noul
    action: flag
    instructions: "Is a dog present?"
    criteria:
      "true": "A dog is present in the state."
      "false": "No dog is present in the state."
  - id: activity
    tier: attestation
    category: risk
    type: score
    action: informational
    instructions: "How much is happening?"
    criteria:
      - "Nothing is happening"
      - "Something is happening"
      - "A great deal is happening"
  - id: animal
    tier: attestation
    category: scope
    type: choice
    action: informational
    instructions: "Which animal is it?"
    criteria:
      dog: "It is a dog."
      cat: "It is a cat."
"""


@pytest.fixture
def fixture_catalog(tmp_path) -> AttestationCatalog:
    path = tmp_path / "attestation_questions.yaml"
    path.write_text(_FIXTURE_CATALOG_YAML, encoding="utf-8")
    return load_catalog(path)


def _make_intent(**overrides) -> Intent:
    kwargs = {
        "agent_id": "agent-jev",
        "description": "Restart the payments deployment in staging",
        "target_resource": "deploy/payments",
        "command": "kubectl rollout restart deploy/payments",
        "working_directory": "/workspace",
        "justification": "Recover from a stuck rollout",
        "blast_radius": "One staging deployment",
    }
    kwargs.update(overrides)
    return Intent.create(**kwargs)


def _synthetic_response(request_body: dict, *, noul_confidence: float | None = None) -> dict:
    """Build a Jev-shaped response mirroring the live contract.

    Per the confirmed hosted-Jev API: noul carries only "noul" (no confidence);
    score/choice carry the value plus "confidence". Whether openjev returns a
    confidence for noul is not yet established, so ``noul_confidence`` lets a
    test emit one and check that a real field is preferred over the proxy.
    """
    answers = {}
    for qid, spec in request_body["questions"].items():
        qtype = spec["type"]
        if qtype == "noul":
            answers[qid] = {"type": "noul", "noul": 0.1}
            if noul_confidence is not None:
                answers[qid]["confidence"] = noul_confidence
        elif qtype == "score":
            crit = spec.get("criteria") or ["a", "b", "c", "d"]
            legend = {str(i): c for i, c in enumerate(crit)}
            probs = {str(i): (1.0 if i == 1 else 0.0) for i in range(len(crit))}
            # level index 1 of len(crit) levels; client normalizes to 1/(len-1)
            answers[qid] = {
                "type": "score",
                "score": 1.0,
                "confidence": 0.9,
                "legend": legend,
                "probabilities": probs,
            }
        else:  # choice
            crit = spec.get("criteria")
            choice = "unknown"
            if isinstance(crit, list) and crit:
                choice = str(crit[0])
            elif isinstance(crit, dict) and crit:
                choice = str(next(iter(crit)))
            answers[qid] = {"type": "choice", "choice": choice, "confidence": 0.9}
    return {
        "model": request_body["model"],
        "answers": answers,
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }


def test_catalog_loads_and_is_wellformed():
    catalog = load_catalog(CATALOG_PATH)
    assert catalog.version
    assert len(catalog.questions) >= 30  # comprehensive catalog
    ids = [q.id for q in catalog.questions]
    assert len(ids) == len(set(ids))  # unique ids
    assert {
        "prompt_injection",
        "social_engineering",
        "credential_exfiltration",
        "disables_security_controls",
        "establishes_persistence",
    }.issubset(set(ids))
    # every security question that could gate a future router is a deny-candidate
    assert any(q.action == "deny-candidate" for q in catalog.questions)
    for question in catalog.questions:
        assert question.qtype in {"noul", "score", "choice"}
        assert question.instructions
        assert question.action in {"informational", "flag", "deny-candidate"}
        assert question.tier in {"triage", "risk", "attestation"}
        assert question.polarity in {"adverse", "safe"}
    # Exactly one safe-polarity question, and it is the triage gate.
    assert catalog.gate.id == "read_only_operation"
    assert [q.id for q in catalog.questions if q.polarity == "safe"] == ["read_only_operation"]
    risk = next(q for q in catalog.questions if q.tier == "risk")
    assert risk.id == "risk_level"
    assert risk.qtype == "choice"
    assert set(risk.criteria) == {"low", "medium", "high", "critical"}


def test_catalog_accepts_either_version_key(tmp_path):
    # The version key is `version` from v0.5.0 and was `catalog_version` before
    # it. The loader takes either, so a catalog revision and a client change can
    # land independently of each other.
    question = (
        "questions:\n  - id: q\n    tier: triage\n    polarity: safe\n    type: noul\n"
        "    category: risk\n    instructions: 'Is it?'\n"
    )
    old = tmp_path / "old.yaml"
    old.write_text(f'catalog_version: "0.4.0"\n{question}', encoding="utf-8")
    new = tmp_path / "new.yaml"
    new.write_text(f'version: "0.5.0"\n{question}', encoding="utf-8")
    assert load_catalog(old).version == "0.4.0"
    assert load_catalog(new).version == "0.5.0"


def test_catalog_rejects_an_unknown_tier(tmp_path):
    # A typo'd tier has to fail loudly: tier is what tells a future router
    # whether an answer is a short-circuit input or council evidence, so
    # silently defaulting one would mis-route it.
    path = tmp_path / "bad.yaml"
    path.write_text(
        'version: "0.5.0"\nquestions:\n  - id: q\n    tier: triaage\n'
        "    type: noul\n    category: risk\n    instructions: 'Is it?'\n",
        encoding="utf-8",
    )
    with pytest.raises(JevEvaluationError, match="invalid tier"):
        load_catalog(path)


_GATE = (
    "  - id: gate\n    tier: triage\n    polarity: safe\n    type: noul\n"
    "    category: risk\n    instructions: 'Is it read-only?'\n"
)


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("", None),
        (_GATE.replace("id: gate", "id: gate2"), "exactly one"),
        (
            "  - id: q\n    polarity: backwards\n    type: noul\n    category: risk\n"
            "    instructions: 'Is it?'\n",
            "invalid polarity",
        ),
        (
            "  - id: q\n    tier: attestation\n    polarity: safe\n    type: noul\n"
            "    category: risk\n    instructions: 'Is it?'\n",
            "exactly one",
        ),
    ],
)
def test_catalog_requires_exactly_one_triage_gate(tmp_path, extra, message):
    # The fast path finds its gate by polarity, never by id, so the loader is
    # what guarantees there is exactly one and that it is a triage noul.
    path = tmp_path / "cat.yaml"
    path.write_text(f'version: "1"\nquestions:\n{_GATE}{extra}', encoding="utf-8")
    if message is None:
        assert load_catalog(path).gate.id == "gate"
    else:
        with pytest.raises(JevEvaluationError, match=message):
            load_catalog(path)


def test_redact_scrubs_common_secrets():
    text = (
        'export AWS_KEY=AKIAABCDEFGHIJKLMNOP; password="hunter2"; '  # pragma: allowlist secret
        "Authorization: Bearer abcdef123456"
    )
    scrubbed = redact(text)
    assert "AKIAABCDEFGHIJKLMNOP" not in scrubbed  # pragma: allowlist secret
    assert "hunter2" not in scrubbed
    assert "abcdef123456" not in scrubbed  # pragma: allowlist secret
    assert "[REDACTED]" in scrubbed


def test_build_state_redacts_and_refuses_oversize():
    intent = _make_intent(context='api_key="sk-secret-value-123"')
    state = build_state(intent, max_chars=10_000)
    assert "sk-secret-value-123" not in state
    assert intent.description in state
    # Never truncated: the chairman and fast path act on this state, and a
    # cut-off tail is where a payload would hide.
    with pytest.raises(JevEvaluationError, match="over the 64 cap"):
        build_state(intent, max_chars=64)


def test_build_state_carries_the_structured_tool_call():
    # Jev must judge the same request the members review: for a structured tool
    # call that is the operation plus its exact arguments, not the empty command.
    from nomos.level_0.types import ToolCallRequest

    tool_call = ToolCallRequest(
        tool_name="billing.refund", arguments={"order": "A-1", "amount": 25}, subject="tenant-1"
    )
    intent = Intent.create(
        agent_id="agent-jev",
        description="Refund one order for a customer",
        target_resource="billing/orders/A-1",
        justification="Customer complaint",
        operation=tool_call.to_operation(),
    )
    intent._tool_review_material = tool_call
    state = json.loads(build_state(intent, max_chars=10_000))
    assert state["operation"]["tool_name"] == "billing.refund"
    assert state["tool_arguments"] == {"order": "A-1", "amount": 25}


def test_build_state_labels_evidence_by_channel_not_text():
    # Trust comes from which field the item arrived in: certified_evidence_rendered
    # is filled only by the council's verification path. Agent text that names
    # the certified label is marked as a claim.
    intent = _make_intent(
        evidence=["trust: certified_executor — git status shows a clean tree"],
        certified_evidence_rendered=["CERTIFIED EXECUTOR EVIDENCE ...\nstdout:\n  main"],
    )
    state = json.loads(build_state(intent, max_chars=10_000, include_evidence=True))
    assert [item["trust"] for item in state["evidence"]] == ["certified_executor", "agent_asserted"]
    assert state["evidence"][0]["content"].startswith("CERTIFIED EXECUTOR EVIDENCE")
    assert "certified_executor (agent-claimed, NOT verified)" in state["evidence"][1]["content"]
    assert "evidence_rules" in state and "certified_executor" in state["evidence_rules"]


def test_build_state_without_evidence_says_so():
    state = json.loads(build_state(_make_intent(), max_chars=10_000, include_evidence=True))
    assert state["evidence"] == []
    assert "No item at all" in state["evidence_rules"]


def test_evidence_is_withheld_from_jev_by_default():
    # jev.include_evidence is off by default: no evidence or rules in the state,
    # and the evidence questions (nothing to judge) are not asked or parsed.
    intent = _make_intent(certified_evidence_rendered=["CERTIFIED EXECUTOR EVIDENCE ..."])
    state = json.loads(build_state(intent, max_chars=10_000))
    assert "evidence" not in state and "evidence_rules" not in state

    catalog = load_catalog(CATALOG_PATH)
    evidence_ids = {q.id for q in catalog.questions if q.category == "evidence"}
    assert evidence_ids == set(JEV_EVIDENCE_QUESTIONS)
    off = JevClient(JevConfig(catalog_path=str(CATALOG_PATH)))
    on = JevClient(JevConfig(catalog_path=str(CATALOG_PATH), include_evidence=True))
    assert not evidence_ids & {q.id for q in off.catalog.questions}
    assert evidence_ids <= {q.id for q in on.catalog.questions}


def test_build_request_shape_matches_catalog():
    catalog = load_catalog(CATALOG_PATH)
    config = JevConfig(catalog_path=str(CATALOG_PATH), model_version="jev-1.13.0")
    request_body = build_request(_make_intent(), catalog, config)
    assert request_body["model"] == "jev-1.13.0"
    assert isinstance(request_body["state"], str)
    assert set(request_body["questions"]) == {q.id for q in catalog.questions}
    for spec in request_body["questions"].values():
        assert spec["type"] in {"noul", "score", "choice"}
        assert "instructions" in spec
        # Jev requires criteria for score (an ordered list) and choice (an
        # object); the catalog must supply it for both.
        if spec["type"] in {"score", "choice"}:
            assert spec.get("criteria")


def test_openjev_sends_noul_criteria_and_typesafe_drops_them(fixture_catalog):
    # The core of the backend adaptation: ONE catalog, two wire formats. openjev
    # accepts criteria on noul, hosted Jev rejects them (log 2026-09-21), so the
    # adapter is what differs — nothing in the catalog is backend-specific.
    intent = _make_intent()
    openjev = build_request(intent, fixture_catalog, JevConfig(provider="openjev", api_key="k"))
    typesafe = build_request(intent, fixture_catalog, JevConfig(provider="typesafe", api_key="k"))
    openjev_questions = openjev["questions"]
    typesafe_questions = typesafe["questions"]

    assert openjev_questions["dog_present"]["criteria"] == {
        "true": "A dog is present in the state.",
        "false": "No dog is present in the state.",
    }
    assert "criteria" not in typesafe_questions["dog_present"]
    # Dropping criteria must not drop the question or its instructions.
    assert typesafe_questions["dog_present"]["type"] == "noul"
    assert (
        typesafe_questions["dog_present"]["instructions"]
        == openjev_questions["dog_present"]["instructions"]
    )

    # score (ordered list) and choice (object) are sent unchanged on both.
    for questions in (openjev_questions, typesafe_questions):
        assert questions["activity"]["criteria"] == [
            "Nothing is happening",
            "Something is happening",
            "A great deal is happening",
        ]
        assert questions["animal"]["criteria"] == {"dog": "It is a dog.", "cat": "It is a cat."}


def test_provider_defaults_resolve_and_an_explicit_value_wins(monkeypatch):
    # Guards the pydantic default trap: the per-provider base_url/model/api_key
    # must be resolved, not passed through literally (a literal ${VAR} would send
    # a bad Bearer token), while an operator's pin must still beat the default.
    monkeypatch.setenv("NVIDIA_API_KEY", "nv-inference-key")  # pragma: allowlist secret
    monkeypatch.setenv("JEV_SECRET", "typesafe-key")  # pragma: allowlist secret

    assert JevConfig().provider == "typesafe"  # the default provider

    openjev = JevConfig(provider="openjev")
    assert openjev.provider == "openjev"
    assert openjev.base_url == "https://inference-api.nvidia.com/openjev"
    assert openjev.endpoint_path == "/v1/systemone"
    assert openjev.model_version == "openjev-4b"  # a pinned version, never an alias
    assert openjev.api_key_env == "NVIDIA_API_KEY"  # pragma: allowlist secret
    assert openjev.api_key == "nv-inference-key"  # pragma: allowlist secret

    typesafe = JevConfig()
    assert typesafe.base_url == "https://api.typesafe.ai"
    assert typesafe.model_version == "jev-1.13.0"
    assert typesafe.api_key_env == "JEV_SECRET"  # pragma: allowlist secret
    assert typesafe.api_key == "typesafe-key"  # pragma: allowlist secret

    pinned = JevConfig(
        provider="openjev",
        base_url="https://jev.internal.example/",
        model_version="openjev-4b-2026-10-01",
        api_key="${JEV_SECRET}",
    )
    assert pinned.base_url == "https://jev.internal.example"  # trailing slash normalized
    assert pinned.model_version == "openjev-4b-2026-10-01"
    assert pinned.api_key == "typesafe-key"  # pragma: allowlist secret

    # A ${VAR} reference that resolves to nothing falls back to the provider
    # default: this is how the chart ships it (COUNCIL_JEV_BASE_URL unset).
    unset = JevConfig(provider="typesafe", base_url="${COUNCIL_JEV_BASE_URL:-}")
    assert unset.base_url == "https://api.typesafe.ai"

    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("JEV_SECRET", raising=False)
    assert JevConfig().api_key == ""


def test_parse_response_normalizes_score(fixture_catalog):
    config = JevConfig(provider="openjev", api_key="k")  # pragma: allowlist secret
    request_body = build_request(_make_intent(), fixture_catalog, config)
    vector = parse_response(
        _synthetic_response(request_body),
        catalog=fixture_catalog,
        requested_version="openjev-4b",
        latency_ms=1.0,
    )
    score_q = next(q for q in fixture_catalog.questions if q.qtype == "score")
    att = vector.by_id(score_q.id)
    assert att is not None
    # synthetic score is index 1 of N levels -> normalized 1/(N-1), value in 0..1
    levels = len(score_q.criteria)
    assert att.value == pytest.approx(1.0 / (levels - 1))
    assert 0.0 <= att.value <= 1.0
    assert att.confidence == pytest.approx(0.9)  # score carries real confidence
    assert att.detail.get("confidence_source") == CONFIDENCE_REPORTED
    assert att.detail.get("raw_score") == 1.0
    assert att.detail.get("label") == score_q.criteria[1]


def test_parse_response_passes_a_choice_through_unchanged(fixture_catalog):
    config = JevConfig(provider="openjev", api_key="k")  # pragma: allowlist secret
    request_body = build_request(_make_intent(), fixture_catalog, config)
    vector = parse_response(
        _synthetic_response(request_body),
        catalog=fixture_catalog,
        requested_version="openjev-4b",
        latency_ms=1.0,
    )
    att = vector.by_id("animal")
    assert att is not None
    assert att.value == "dog"  # the chosen option string, not a number
    assert att.confidence == pytest.approx(0.9)
    assert att.detail.get("confidence_source") == CONFIDENCE_REPORTED


def test_noul_confidence_prefers_a_real_field_over_the_proxy(fixture_catalog):
    # Whether openjev returns a confidence for noul is unprobed. When it does,
    # the real number must be used and marked 'reported'; when it does not, the
    # |2p-1| proxy stands in and must be marked as a proxy so a threshold can
    # never silently treat a derived number as a calibrated one.
    config = JevConfig(provider="openjev", api_key="k")  # pragma: allowlist secret
    request_body = build_request(_make_intent(), fixture_catalog, config)

    reported = parse_response(
        _synthetic_response(request_body, noul_confidence=0.42),
        catalog=fixture_catalog,
        requested_version="openjev-4b",
        latency_ms=1.0,
    ).by_id("dog_present")
    assert reported is not None
    assert reported.confidence == pytest.approx(0.42)
    assert reported.detail["confidence_source"] == CONFIDENCE_REPORTED

    derived = parse_response(
        _synthetic_response(request_body),
        catalog=fixture_catalog,
        requested_version="openjev-4b",
        latency_ms=1.0,
    ).by_id("dog_present")
    assert derived is not None
    assert derived.confidence == pytest.approx(0.8)  # p=0.1 -> |2p-1| = 0.8
    assert derived.detail["confidence_source"] == CONFIDENCE_CERTAINTY_PROXY


def test_parse_response_rejects_a_missing_answer(fixture_catalog):
    # An unanswered question is a contract failure, not a zero: dropping it
    # would quietly shrink the vector that evidence and future routing read.
    config = JevConfig(provider="openjev", api_key="k")  # pragma: allowlist secret
    request_body = build_request(_make_intent(), fixture_catalog, config)
    payload = _synthetic_response(request_body)
    del payload["answers"]["dog_present"]
    with pytest.raises(JevEvaluationError, match="dog_present"):
        parse_response(
            payload,
            catalog=fixture_catalog,
            requested_version="openjev-4b",
            latency_ms=1.0,
        )


def test_parse_response_builds_vector():
    catalog = load_catalog(CATALOG_PATH)
    config = JevConfig(catalog_path=str(CATALOG_PATH))
    request_body = build_request(_make_intent(), catalog, config)
    payload = _synthetic_response(request_body)

    vector = parse_response(
        payload, catalog=catalog, requested_version=config.model_version, latency_ms=42.0
    )
    assert isinstance(vector, AttestationVector)
    assert vector.model_version == config.model_version
    assert vector.catalog_version == catalog.version
    assert vector.outcome == "pass"  # the vector itself never denies
    assert len(vector.attestations) == len(catalog.questions)
    inj = vector.by_id("prompt_injection")
    assert inj is not None
    assert inj.value == pytest.approx(0.1)
    # noul carries no confidence here; the parser derives the |2p-1| proxy.
    assert inj.confidence == pytest.approx(0.8)
    assert inj.detail["confidence_source"] == CONFIDENCE_CERTAINTY_PROXY
    assert vector.usage.get("input_tokens") == 100


def test_missing_api_key_names_the_providers_key_variable(monkeypatch, fixture_catalog):
    # The operator-facing half of the provider switch: the two providers read
    # different variables out of the same deployment Secret, so the error has to
    # say which one this provider wanted.
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("JEV_SECRET", raising=False)
    openjev = JevClient(JevConfig(provider="openjev"), catalog=fixture_catalog)
    with pytest.raises(JevEvaluationError, match="NVIDIA_API_KEY"):
        asyncio.run(openjev.evaluate(_make_intent()))
    typesafe = JevClient(JevConfig(provider="typesafe"), catalog=fixture_catalog)
    with pytest.raises(JevEvaluationError, match="JEV_SECRET"):
        asyncio.run(typesafe.evaluate(_make_intent()))


class _StubJevHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        request_body = json.loads(self.rfile.read(length).decode("utf-8"))
        body = json.dumps(_synthetic_response(request_body)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@pytest.mark.parametrize("provider", ["openjev", "typesafe"])
def test_client_evaluate_end_to_end(provider):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubJevHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        config = JevConfig(
            enabled=True,
            provider=provider,
            base_url=f"http://{host}:{port}",
            api_key="test-key",  # pragma: allowlist secret
            catalog_path=str(CATALOG_PATH),
        )
        client = JevClient(config)
        vector = asyncio.run(client.evaluate(_make_intent()))
        assert len(vector.attestations) == len(client.catalog.questions)
        assert vector.latency_ms >= 0.0
        # The vector records the provider's pinned model version.
        assert vector.model_version == config.model_version
    finally:
        server.shutdown()
        thread.join()


def test_question_batch_size_follows_the_provider():
    # openjev rejects more than 16 questions per request (HTTP 422, live
    # 2026-10-26); hosted Jev takes the whole catalog in one call.
    assert JevConfig(provider="openjev", api_key="k").question_batch_size == 16
    assert JevConfig(provider="typesafe", api_key="k").question_batch_size is None
    pinned = JevConfig(provider="typesafe", api_key="k", max_questions_per_request=5)
    assert pinned.question_batch_size == 5


class _CountingJevHandler(_StubJevHandler):
    sizes: ClassVar[list[int]] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        request_body = json.loads(self.rfile.read(length).decode("utf-8"))
        type(self).sizes.append(len(request_body["questions"]))
        body = json.dumps(_synthetic_response(request_body)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_openjev_catalog_is_batched_and_merged():
    _CountingJevHandler.sizes = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CountingJevHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        config = JevConfig(
            enabled=True,
            provider="openjev",
            base_url=f"http://{host}:{port}",
            api_key="test-key",  # pragma: allowlist secret
            catalog_path=str(CATALOG_PATH),
        )
        client = JevClient(config)
        vector = asyncio.run(client.evaluate(_make_intent()))
        total = len(client.catalog.questions)
        assert sorted(_CountingJevHandler.sizes, reverse=True)[0] == 16
        assert sum(_CountingJevHandler.sizes) == total
        assert len(_CountingJevHandler.sizes) == -(-total // 16)
        assert len(vector.attestations) == total
        # usage is summed across batches
        assert vector.usage["input_tokens"] == 100 * len(_CountingJevHandler.sizes)
    finally:
        server.shutdown()
        thread.join()
