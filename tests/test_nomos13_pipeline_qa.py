# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Independent NOMOS-13 acceptance: frozen design P01-P06/P08 only.

No product source/test reads. Run with PYTHONPATH pointing at the checkout under
assessment. Public runtime reflection settled existing fixture adapter shapes.
The fallback context is solely a frozen-design fixture allowing behavioral red
on base (missing feature imports never count as behavioral evidence).
"""

from __future__ import annotations

import base64
import copy
import hashlib
import importlib
import json
from dataclasses import dataclass
from unittest.mock import Mock

import pytest
import yaml

from nomos.auth.authenticator import AgentAuthenticator, AgentIdentity
from nomos.auth.writ_factory import WritFactory
from nomos.config.settings import CouncilMemberConfig, CouncilSettings, load_council_catalog
from nomos.level_0.types.intent import Intent
from nomos.level_0.types.packaged_file import PackagedFile
from nomos.level_0.types.tool_call import ToolCallRequest
from nomos.llm.base import LLMResponse
from nomos.llm.council_agent import (
    ChairmanAgent,
    CodeReviewAgent,
    CouncilMemberAgent,
)
from nomos.nat.functions import AuthorizeInput, CouncilService, ProposeIntentInput
from nomos.nat.registry import CouncilRegistry
from nomos.opa.client import OPAHttpClient, OPAPolicyDecision, build_intent_filter_input

TOOL = "warehouse.delete_rows"
MARK_A = "OPERATOR_A_ONLY_DELETE_SCRATCH"
MARK_B = "OPERATOR_B_ONLY_DELETE_PRODUCTION"
FORGED = "CLIENT_FORGED_TRUSTED_DEFINITION"
ARG_MARK = "BUSINESS_ARGUMENT_LITERAL_qa_7041"
CUSTOM = "CUSTOM_OPERATOR_SYSTEM_PROMPT_qa_7041"
OMITTED = object()


def definition(*, table="scratch", marker=MARK_A, maximum=5):
    return {
        "name": TOOL,
        "description": marker + ": permanently delete selected warehouse rows.",
        "parameters": {
            "type": "object",
            "properties": {
                "table": {"type": "string", "const": table},
                "limit": {"type": "integer", "minimum": 1, "maximum": maximum},
                "options": {
                    "type": "object",
                    "properties": {"labels": {"type": "array", "items": {"type": "string"}}},
                    "additionalProperties": False,
                },
            },
            "required": ["table", "limit"],
            "additionalProperties": False,
        },
        "usage_constraints": [marker + ": require explicit consent and inspect rows first."],
    }


def arguments(*, table="scratch", limit=2):
    return {"table": table, "limit": limit, "options": {"labels": [ARG_MARK, "Ω"]}}


def tool_call(*, name=TOOL, args=None):
    return ToolCallRequest(
        tool_name=name, subject="warehouse:scratch", arguments=arguments() if args is None else args
    )


def intent_for(call=None, *, council="qa_a", forged=False):
    i = Intent(
        intent_id="intent-qa-test",
        agent_id="qa_agent",
        council_id=council,
        description="Inspect scratch rows safely",
        target_resource="/tmp/scratch",
        command="" if call else "ls /tmp/scratch",
        working_directory="/tmp",
        operation=call.to_operation() if call else None,
        context=FORGED if forged else "",
        metadata={"trusted_tool_context": FORGED, "tools": [FORGED]} if forged else {},
    )
    if call is not None:
        i._tool_review_material = call
    return i


@dataclass(frozen=True)
class _FrozenContractContext:
    council_id: str
    catalog_json: str
    matched_tool_name: str | None
    arguments_json: str | None
    operation: object | None

    def to_public_dict(self):
        tools = json.loads(self.catalog_json)
        return {
            "council_id": self.council_id,
            "tools": tools,
            "matched_tool": next((x for x in tools if x["name"] == self.matched_tool_name), None),
            "arguments": json.loads(self.arguments_json) if self.arguments_json else None,
            "operation": self.operation.model_dump(mode="json") if self.operation else None,
        }


def context_for(*, catalog=None, call=None, council="qa_a"):
    # A missing feature module on base must yield observable behavior failures,
    # not terminate collection with an import error.
    try:
        context_type = importlib.import_module(
            "nomos.level_0.types.tool_definition"
        ).TrustedToolReviewContext
    except ModuleNotFoundError as exc:
        if exc.name != "nomos.level_0.types.tool_definition":
            raise
        context_type = _FrozenContractContext
    return context_type(
        council_id=council,
        catalog_json=json.dumps(
            [definition()] if catalog is None else catalog,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ),
        matched_tool_name=call.tool_name if call else None,
        arguments_json=call.canonical_arguments().decode() if call else None,
        operation=call.to_operation() if call else None,
    )


def attach(i, context):
    i._trusted_tool_context = context
    return i


def opa_doc(i, *, request_type="authorize"):
    return build_intent_filter_input(
        i,
        request_type=request_type,
        authenticated=True,
        agent_permissions=["tool:write"],
        agent_councils=[i.council_id],
        required_permissions=["tool:write"],
    )


def contains_json(text, expected):
    """Compare full JSON structure without assuming prompt whitespace/labels."""

    def includes(value):
        if value == expected:
            return True
        if isinstance(value, dict):
            return any(includes(v) for v in value.values())
        if isinstance(value, list):
            return any(includes(v) for v in value)
        return False

    decoder = json.JSONDecoder()
    for offset, ch in enumerate(text):
        if ch not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(text[offset:])
        except ValueError:
            continue
        if includes(value):
            return True
    return False


class RecordingBackend:
    def __init__(self, decision="approve"):
        self.calls = []
        self.decision = decision

    def provider_name(self):
        return "independent-pipeline-qa"

    async def complete(self, messages, **kwargs):
        self.calls.append(copy.deepcopy(messages))
        response = {
            "decision": self.decision,
            "reasoning": "Independent QA fixture checks explicit authorized scratch action.",
            "risk_concerns": [],
            "conditions": [],
            "guidance": "",
            "threshold": "unanimous",
            "risk_level": "low",
        }
        if any("Packaged File Code Review" in m.content for m in messages):
            response.update(
                reviewed_all_content=True,
                claim_match="yes",
                overall_risk="low",
                files=[
                    {
                        "path": "qa-scratch.txt",
                        "sha256": hashlib.sha256(b"QA scratch text\n").hexdigest(),
                        "mode": "0600",
                        "review_status": "complete",
                        "matches_claimed_behavior": "yes",
                        "observed_operations": ["Contains QA scratch text"],
                        "risk_findings": [],
                        "evidence": ["QA scratch text"],
                    }
                ],
            )
        return LLMResponse(content=json.dumps(response))


def config_dict(
    council,
    tools=OMITTED,
    *,
    opa=True,
    fail_closed=True,
    require_auth=False,
    members=1,
    chairman=False,
):
    d = {
        "council_id": council,
        "require_auth": require_auth,
        "require_council_membership": require_auth,
        "require_justification": False,
        "llm_max_retries": 0,
        "llm_backends": {"qa": {"provider": "openai", "model": "fixture-only"}},
        "members": [
            {"id": f"qa-member-{n}", "backend": "qa", "system_prompt_override": CUSTOM}
            for n in range(members)
        ],
        "chairman": {
            "enabled": chairman,
            "backend": "qa" if chairman else "",
            "system_prompt_override": CUSTOM,
        },
        "opa": {"enabled": opa, "fail_closed": fail_closed},
    }
    if tools is not OMITTED:
        d["tools"] = copy.deepcopy(tools)
    return d


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """Invoke real service/gates; replace only public external boundaries."""
    backend = RecordingBackend()
    opa_calls = []
    mint_calls = []
    reviewer_calls = []
    settings_instances = []
    opa_state = {"allow": True, "raise": False, "mutate": False}
    identity = {"value": None}
    original_mint = WritFactory.mint_writ

    def create_backend(*args, **kwargs):
        return backend

    async def evaluate(self, doc):
        opa_calls.append(copy.deepcopy(doc))
        if opa_state["mutate"] and "tools" in doc.get("council", {}):
            doc["council"]["tools"][0]["description"] = FORGED
            doc["intent"]["tool_definition"]["parameters"]["properties"]["limit"]["maximum"] = 999
            doc["intent"]["tool_arguments"]["options"]["labels"].append(FORGED)
        if opa_state["raise"]:
            from nomos.opa.client import OPAEvaluationError

            raise OPAEvaluationError("Independent QA unavailable OPA fixture")
        return OPAPolicyDecision(
            allow=opa_state["allow"], reasons=[] if opa_state["allow"] else ["QA policy deny"]
        )

    def mint(self, *args, **kwargs):
        mint_calls.append((args, kwargs))
        return original_mint(self, *args, **kwargs)

    monkeypatch.setattr("nomos.nat.functions.create_backend", create_backend)
    monkeypatch.setattr("nomos.llm.council_agent.create_backend", create_backend)
    monkeypatch.setattr(OPAHttpClient, "evaluate", evaluate)
    monkeypatch.setattr(WritFactory, "mint_writ", mint)

    def recording_review(method, name):
        async def recorded(self, *args, **kwargs):
            reviewer_calls.append(name)
            return await method(self, *args, **kwargs)

        return recorded

    for cls, name in [
        (CouncilMemberAgent, "evaluate_intent"),
        (CouncilMemberAgent, "debate_evaluate"),
        (ChairmanAgent, "decide_threshold"),
        (ChairmanAgent, "pre_authorize"),
        (CodeReviewAgent, "review_intent_files"),
    ]:
        monkeypatch.setattr(cls, name, recording_review(getattr(cls, name), name))

    async def authenticate(self, token):
        return identity["value"]

    async def init_stores(self):
        return None

    monkeypatch.setattr(AgentAuthenticator, "authenticate", authenticate)
    monkeypatch.setattr(CouncilService, "init_stores", init_stores)
    monkeypatch.setattr(CouncilService, "init_redis", init_stores)

    def service(tools=OMITTED, *, council="qa_a", **kwargs):
        raw = config_dict(council, tools, **kwargs)
        raw["council_key_path"] = str(tmp_path / f"keys-{council}")
        settings = CouncilSettings.model_validate(raw)
        settings_instances.append(settings)
        s = CouncilService(settings)
        if kwargs.get("require_auth"):
            # Existing runtime store adapter: authenticate is public and stubbed;
            # real pure permission/membership checks remain active.
            s._authenticator = AgentAuthenticator(Mock())
        return s

    def clear():
        backend.calls.clear()
        opa_calls.clear()
        mint_calls.clear()
        reviewer_calls.clear()

    return type(
        "Harness",
        (),
        {
            "backend": backend,
            "opa_calls": opa_calls,
            "mint_calls": mint_calls,
            "reviewer_calls": reviewer_calls,
            "settings_instances": settings_instances,
            "opa_state": opa_state,
            "identity": identity,
            "service": staticmethod(service),
            "clear": staticmethod(clear),
        },
    )()


def authorize_request(call=None, *, council="qa_a", api_version="", **extra):
    data = dict(
        agent_id="qa_agent",
        council_id=council,
        api_version=api_version,
        description="Explicit authorized scratch action",
        justification="User explicitly authorized scratch work",
        **extra,
    )
    if call is None:
        data.update(command="ls /tmp/scratch", working_directory="/tmp")
    else:
        data["tool_call"] = call
    return AuthorizeInput.model_validate(data)


INVALID_CALLS = [
    ("unknown", lambda: tool_call(name="warehouse.new_delete")),
    ("case", lambda: tool_call(name="Warehouse.delete_rows")),
    ("maximum", lambda: tool_call(args=arguments(limit=6))),
    ("wrong_type", lambda: tool_call(args=arguments(limit="2"))),
    ("missing_required", lambda: tool_call(args={"table": "scratch"})),
    ("extra_argument", lambda: tool_call(args={**arguments(), "force": True})),
    ("nested_argument", lambda: tool_call(args={**arguments(), "options": {"force": True}})),
    ("different_table", lambda: tool_call(args=arguments(table="production"))),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "opa,fail_closed,unavailable", [(False, True, False), (True, True, False), (True, False, True)]
)
@pytest.mark.parametrize("case,call_factory", INVALID_CALLS, ids=[x[0] for x in INVALID_CALLS])
async def test_p01_invalid_calls_stop_before_opa_review_and_mint(
    harness, opa, fail_closed, unavailable, case, call_factory
):
    s = harness.service([definition()], opa=opa, fail_closed=fail_closed)
    harness.opa_state["raise"] = unavailable
    result = await s.authorize(authorize_request(call_factory()))
    # Forbidden effects checked before result shape: cannot pass on unrelated denial.
    observed = dict(
        status=result.status,
        writ=bool(result.writ),
        opa=len(harness.opa_calls),
        reviewers=len(harness.reviewer_calls),
        mint=len(harness.mint_calls),
    )
    assert harness.opa_calls == [], f"Invalid catalog call reached OPA: {observed}"
    assert harness.reviewer_calls == [], f"Invalid catalog call invoked a reviewer: {observed}"
    assert harness.backend.calls == [], "Invalid catalog call reached a reviewer backend"
    assert harness.mint_calls == [], "Invalid catalog call reached signed mint"
    assert result.status == "error"
    assert result.writ is None and not result.writ_id
    assert result.error.startswith("Invalid tool call:")


@pytest.mark.asyncio
async def test_p01_empty_catalog_denies_structured_without_side_effects(harness):
    result = await harness.service([]).authorize(authorize_request(tool_call()))
    assert not harness.opa_calls and not harness.backend.calls and not harness.mint_calls
    assert result.status == "error" and result.writ is None
    assert result.error.startswith("Invalid tool call:")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "opa,fail_closed,unavailable", [(False, True, False), (True, True, False), (True, False, True)]
)
async def test_control_valid_catalog_reaches_existing_gates(harness, opa, fail_closed, unavailable):
    harness.opa_state["raise"] = unavailable
    result = await harness.service([definition()], opa=opa, fail_closed=fail_closed).authorize(
        authorize_request(tool_call())
    )
    assert bool(harness.opa_calls) == opa
    assert harness.backend.calls, result.error
    assert harness.mint_calls, result.error
    assert harness.reviewer_calls, result.error
    assert result.status == "approved" and result.writ


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["shell", "propose"])
@pytest.mark.parametrize("catalog", [[definition()], []], ids=["populated", "empty"])
async def test_p02_advisory_and_shell_operator_availability_resists_client_forgery(
    harness, path, catalog
):
    s = harness.service(catalog)
    forged = {"council_id": "qa_b", "tools": [FORGED], "matched_tool": FORGED}
    extras = {
        "context": FORGED,
        "evidence": [FORGED],
        "metadata": {"tools": [FORGED], "trusted_tool_context": forged},
        "_trusted_tool_context": forged,
        "trusted_tool_context": forged,
        "tools": [FORGED],
    }
    harness.opa_state["allow"] = False
    if path == "shell":
        result = await s.authorize(authorize_request(**extras))
        assert result.writ is None
    else:
        request = ProposeIntentInput.model_validate(
            dict(
                agent_id="qa_agent",
                council_id="qa_a",
                description="Client claim " + FORGED,
                target_resource="/tmp/scratch",
                command="ls /tmp/scratch",
                working_directory="/tmp",
                tool_call=tool_call().model_dump(mode="json"),
                **extras,
            )
        )
        assert "tool_call" not in type(request).model_fields
        result = await s.propose_intent(request)
        assert result.success and result.state == "denied"
    assert len(harness.opa_calls) == 1
    doc = harness.opa_calls[0]
    assert doc["council"]["tools"] == catalog
    assert doc["intent"]["tool_definition"] is None
    assert doc["intent"]["tool_arguments"] is None
    assert doc["intent"]["operation"] is None
    assert not harness.mint_calls


STAGES = ["member", "debate", "chairman", "preauthorization", "packaged_files"]


async def reviewer_call(stage, i, backend):
    if stage in ("member", "debate"):
        agent = CouncilMemberAgent(
            CouncilMemberConfig(id="qa-member", backend="qa", system_prompt_override=CUSTOM),
            backend,
            max_retries=0,
        )
        return await (
            agent.evaluate_intent(i)
            if stage == "member"
            else agent.debate_evaluate(i, "Review again using only explicit user authority.")
        )
    if stage in ("chairman", "preauthorization"):
        agent = ChairmanAgent(backend, system_prompt=CUSTOM, max_retries=0)
        return await (
            agent.decide_threshold(i)
            if stage == "chairman"
            else agent.pre_authorize(i, {"tool:write"}, {"tool:write"})
        )
    i.packaged_files = [
        PackagedFile(
            path="qa-scratch.txt", content_base64=base64.b64encode(b"QA scratch text\n").decode()
        )
    ]
    agent = CodeReviewAgent(backend, system_prompt=CUSTOM, max_retries=0)
    return await agent.review_intent_files(i)


def assert_trusted_messages(messages, catalog, *, expect_args=False):
    systems = [m.content for m in messages if str(m.role) == "system"]
    users = [m.content for m in messages if str(m.role) == "user"]
    assert CUSTOM in systems, "Operator custom system prompt was lost"
    trusted = [s for s in systems if contains_json(s, catalog)]
    assert trusted, "Full trusted catalog missing from separate SYSTEM message"
    assert all(s != CUSTOM for s in trusted), "Definitions must have their own SYSTEM message"
    assert all(FORGED not in s for s in systems)
    assert all(ARG_MARK not in s for s in systems), "Live business arguments belong in USER"
    if expect_args:
        assert any(contains_json(s, arguments()) for s in users), (
            "Exact business arguments absent from USER"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("structured", [False, True], ids=["availability", "matched"])
async def test_p03_every_llm_stage_gets_separate_trusted_system_with_custom_prompt(
    stage, structured
):
    call = tool_call() if structured else None
    i = attach(intent_for(call, forged=True), context_for(call=call))
    backend = RecordingBackend()
    await reviewer_call(stage, i, backend)
    assert backend.calls
    assert_trusted_messages(backend.calls[0], [definition()], expect_args=structured)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", STAGES)
async def test_control_no_catalog_llm_sequence_is_existing_system_user(stage):
    backend = RecordingBackend()
    await reviewer_call(stage, intent_for(tool_call()), backend)
    assert backend.calls
    assert [str(m.role) for m in backend.calls[0]] == ["system", "user"]
    assert backend.calls[0][0].content == CUSTOM
    assert MARK_A not in "".join(m.content for m in backend.calls[0])
    assert contains_json(backend.calls[0][1].content, arguments())


def test_p02_intent_inputs_cannot_create_trusted_private_context():
    forged = {"council_id": "qa_b", "tools": [FORGED]}
    data = intent_for().model_dump(mode="json")
    data.update(
        _trusted_tool_context=forged,
        trusted_tool_context=forged,
        tools=[FORGED],
        metadata={"_trusted_tool_context": forged},
    )
    i = Intent.model_validate(data)
    assert getattr(i, "_trusted_tool_context", None) is None
    assert "tools" not in opa_doc(i)["council"]
    assert "tool_definition" not in opa_doc(i)["intent"]


def test_p03_private_context_is_declared_preserved_in_copy_and_excluded_from_dumps():
    assert "_trusted_tool_context" in Intent.__private_attributes__
    call = tool_call()
    i = attach(intent_for(call), context_for(call=call))
    shallow, deep = i.model_copy(), i.model_copy(deep=True)
    for copied in (shallow, deep):
        assert (
            copied._trusted_tool_context.to_public_dict()
            == i._trusted_tool_context.to_public_dict()
        )
        assert MARK_A not in json.dumps(copied.model_dump(mode="json"))
        assert ARG_MARK not in json.dumps(copied.model_dump(mode="json"))


@pytest.mark.parametrize(
    "request_type,structured,catalog",
    [
        ("authorize", True, [definition()]),
        ("authorize", False, [definition()]),
        ("propose_intent", False, [definition()]),
        ("authorize", False, []),
        ("propose_intent", False, []),
    ],
)
def test_p04_opa_exact_additive_fields_and_deep_copy(request_type, structured, catalog):
    call = tool_call() if structured else None
    plain = intent_for(call)
    legacy = opa_doc(plain, request_type=request_type)
    i = attach(intent_for(call), context_for(catalog=catalog, call=call))
    out = opa_doc(i, request_type=request_type)
    expected_args = arguments() if call else None
    assert out["council"]["tools"] == catalog
    assert out["intent"]["tool_definition"] == (definition() if call else None)
    assert out["intent"]["tool_arguments"] == expected_args
    assert out["intent"]["operation"] == (
        call.to_operation().model_dump(mode="json") if call else None
    )
    old_shape = copy.deepcopy(out)
    del old_shape["council"]["tools"]
    del old_shape["intent"]["tool_definition"]
    del old_shape["intent"]["tool_arguments"]
    assert old_shape == legacy, "NOMOS-13 changed existing OPA fields"
    snapshot = i._trusted_tool_context.to_public_dict()
    if catalog:
        out["council"]["tools"][0]["parameters"]["properties"]["limit"]["maximum"] = 999
    if call:
        out["intent"]["tool_definition"]["description"] = FORGED
        out["intent"]["tool_arguments"]["options"]["labels"].append(FORGED)
    assert i._trusted_tool_context.to_public_dict() == snapshot
    again = opa_doc(i, request_type=request_type)
    assert again["council"]["tools"] == catalog
    assert again["intent"]["tool_arguments"] == expected_args
    if call:
        assert call.arguments == expected_args


def test_control_no_catalog_opa_full_legacy_shape():
    expected = {
        "request": {"type": "authorize", "authenticated": True},
        "council": {
            "id": "qa_a",
            "opa_policy_id": "strict-shell",
            "opa_relaxed_bare_command_words": [],
            "opa_relaxed_working_dir_prefixes": [],
        },
        "agent": {"id": "qa_agent", "permissions": ["tool:write"], "councils": ["qa_a"]},
        "required_permissions": ["tool:write"],
        "intent": {
            "intent_id": "intent-qa-test",
            "agent_id": "qa_agent",
            "council_id": "qa_a",
            "action_type": "system.exec",
            "description": "Inspect scratch rows safely",
            "target_resource": "/tmp/scratch",
            "command": "ls /tmp/scratch",
            "command_type": "shell",
            "working_directory": "/tmp",
            "target_host": "",
            "target_environment": "",
            "target_cluster": "",
            "target_namespace": "",
            "target_container": "",
            "operation": None,
            "justification": "",
            "blast_radius": "",
            "context": "",
            "shell_command_words": ["ls"],
            "normalized_path_refs": ["/tmp", "/tmp/scratch"],
            "packaged_files": [],
            "evidence_count": 0,
            "has_rollback_plan": False,
        },
    }
    assert opa_doc(intent_for()) == expected


@pytest.mark.parametrize("mismatch", ["council", "operation"])
def test_p04_opa_rejects_mismatched_server_context(mismatch):
    call = tool_call()
    i = intent_for(call)
    ctx = context_for(call=call, council="qa_b" if mismatch == "council" else "qa_a")
    attach(i, ctx)
    if mismatch == "operation":
        i.operation = tool_call(args=arguments(limit=3)).to_operation()
    with pytest.raises(ValueError):
        opa_doc(i)


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["council", "operation"])
async def test_p03_review_mismatch_never_calls_backend(mismatch):
    call = tool_call()
    i = attach(
        intent_for(call),
        context_for(call=call, council="qa_b" if mismatch == "council" else "qa_a"),
    )
    if mismatch == "operation":
        i.operation = tool_call(args=arguments(limit=3)).to_operation()
    backend = RecordingBackend()
    try:
        result = await reviewer_call("member", i, backend)
    except ValueError:
        pass
    else:
        assert result.decision != "approve"
    assert not backend.calls


@pytest.mark.asyncio
async def test_p04_mutating_opa_client_cannot_change_later_review_or_signed_operation(harness):
    harness.opa_state["mutate"] = True
    call = tool_call()
    result = await harness.service([definition()]).authorize(authorize_request(call))
    assert result.status == "approved", result.error
    assert harness.opa_calls[0]["intent"]["tool_arguments"] == arguments()
    assert_trusted_messages(harness.backend.calls[0], [definition()], expect_args=True)
    signed = WritFactory.deserialize_writ(result.writ)
    assert signed.payload().operation == call.to_operation()
    assert FORGED not in "".join(
        m.content for m in harness.backend.calls[0] if str(m.role) == "system"
    )


@pytest.mark.asyncio
async def test_p05_same_name_councils_are_isolated(harness):
    a = harness.service([definition()], council="qa_a")
    b = harness.service([definition(table="production", marker=MARK_B)], council="qa_b")
    denied = await b.authorize(authorize_request(tool_call(), council="qa_b"))
    assert denied.status == "error" and not denied.writ
    assert not harness.opa_calls and not harness.backend.calls and not harness.mint_calls
    ok_a = await a.authorize(authorize_request(tool_call(), council="qa_a"))
    ok_b = await b.authorize(
        authorize_request(tool_call(args=arguments(table="production")), council="qa_b")
    )
    assert ok_a.status == ok_b.status == "approved"
    assert harness.opa_calls[0]["council"]["tools"] == [definition()]
    assert harness.opa_calls[1]["council"]["tools"] == [
        definition(table="production", marker=MARK_B)
    ]
    assert MARK_B not in "".join(m.content for m in harness.backend.calls[0])
    assert MARK_A not in "".join(m.content for m in harness.backend.calls[1])


def write_catalog(path, tools):
    raw = config_dict("qa_a", tools)
    raw["council_key_path"] = str(path.parent / "registry-keys-pipeline_qa")
    path.write_text(yaml.safe_dump({"default_council_id": "qa_a", "councils": {"qa_a": raw}}))


@pytest.mark.asyncio
async def test_p05_registry_reload_removal_invalid_and_inflight_snapshots(harness, tmp_path):
    path = tmp_path / "catalog-pipeline_qa.yaml"
    write_catalog(path, [definition()])
    registry = CouncilRegistry(load_council_catalog(str(path)), catalog_path=path)
    old = await registry.get("qa_a")
    write_catalog(path, [definition(maximum=1, marker=MARK_A + "_REV2")])
    new = await registry.get("qa_a")
    assert new is not old
    result = await new.authorize(authorize_request(tool_call()))
    assert result.status == "error" and not result.writ
    assert not harness.opa_calls and not harness.backend.calls and not harness.mint_calls
    old_ok = await old.authorize(authorize_request(tool_call()))
    assert old_ok.status == "approved"
    assert harness.opa_calls[-1]["council"]["tools"] == [definition()]
    harness.clear()
    write_catalog(path, [])
    removed = await registry.get("qa_a")
    assert (
        await removed.authorize(authorize_request(tool_call(args=arguments(limit=1))))
    ).status == "error"
    assert not harness.opa_calls and not harness.backend.calls and not harness.mint_calls
    write_catalog(path, None)
    with pytest.raises((ValueError, RuntimeError)):
        await registry.get("qa_a")
    # An invalid acquisition cannot destroy an already held snapshot.
    assert (await old.authorize(authorize_request(tool_call()))).status == "approved"


@pytest.mark.asyncio
async def test_p05_service_owns_snapshot_of_validated_settings(harness):
    raw = definition()
    s = harness.service([raw])
    # Mutating operator caller input after construction cannot rewrite a request.
    raw["description"] = FORGED
    raw["parameters"]["properties"]["limit"]["maximum"] = 1000
    # Deeply immutable definitions may explicitly refuse nested mutation.
    # This does not excuse aliasing the mutable settings catalog container.
    settings = harness.settings_instances[-1]
    tools = getattr(settings, "tools", None)
    if tools:
        for property_name, key, value in [
            ("limit", "maximum", 1000),
            ("table", "const", "production"),
        ]:
            node = tools[0].parameters["properties"][property_name]
            original = node[key]
            try:
                node[key] = value
            except TypeError:
                assert node[key] == original, "Refused mutation changed the definition"
        replacement = CouncilSettings.model_validate(
            config_dict("qa_a", [definition(maximum=1000, marker=FORGED)])
        )
        if isinstance(tools, list):
            tools[:] = replacement.tools
        settings.tools = replacement.tools
        assert settings.tools[0].parameters["properties"]["limit"]["maximum"] == 1000
    result = await s.authorize(authorize_request(tool_call(args=arguments(limit=6))))
    assert result.status == "error" and not result.writ
    assert not harness.opa_calls and not harness.backend.calls and not harness.mint_calls


@pytest.mark.asyncio
async def test_p06_catalog_does_not_override_opa_deny(harness):
    harness.opa_state["allow"] = False
    result = await harness.service([definition()]).authorize(authorize_request(tool_call()))
    assert result.status == "denied" and not result.writ
    assert harness.opa_calls and not harness.backend.calls and not harness.mint_calls


@pytest.mark.asyncio
async def test_p06_catalog_does_not_override_reviewer_deny(harness):
    harness.backend.decision = "deny"
    result = await harness.service([definition()]).authorize(authorize_request(tool_call()))
    assert result.status == "denied" and not result.writ
    assert harness.backend.calls and not harness.mint_calls


@pytest.mark.asyncio
async def test_p06_one_approval_cannot_replace_unanimous_quorum(harness):
    original = harness.backend.complete
    calls = {"n": 0}

    async def split(messages, **kwargs):
        calls["n"] += 1
        harness.backend.decision = "approve" if calls["n"] == 1 else "deny"
        return await original(messages, **kwargs)

    harness.backend.complete = split
    result = await harness.service([definition()], members=2).authorize(
        authorize_request(tool_call())
    )
    assert len(harness.backend.calls) == 2
    assert result.status == "denied" and not result.writ and not harness.mint_calls


@pytest.mark.asyncio
async def test_p06_catalog_never_grants_missing_tool_write_permission(harness):
    harness.identity["value"] = AgentIdentity(
        agent_id="qa_agent",
        display_name="Independent QA identity",
        permissions={"council:petition", "system:read"},
        councils={"qa_a"},
    )
    s = harness.service([definition()], require_auth=True)
    result = await s.authorize(authorize_request(tool_call(), auth_token="qa-fixture-token"))
    assert result.status == "denied" and not result.writ
    assert "tool:write" in result.missing_permissions
    assert not harness.backend.calls and not harness.mint_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["shell", "structured", "legacy_name"])
async def test_p08_control_omitted_catalog_preserves_existing_authorization_and_shape(
    harness, kind
):
    call = (
        None
        if kind == "shell"
        else tool_call(name="legacy bespoke tool with spaces" if kind == "legacy_name" else TOOL)
    )
    request = authorize_request(call)
    result = await harness.service().authorize(request)
    assert result.status == "approved" and result.writ
    assert harness.mint_calls and harness.backend.calls
    doc = harness.opa_calls[0]
    assert "tools" not in doc["council"]
    assert "tool_arguments" not in doc["intent"] and "tool_definition" not in doc["intent"]
    assert [str(m.role) for m in harness.backend.calls[0]] == ["system", "user"]
    assert result.api_version == ("v2" if call else "v1")
    signed = WritFactory.deserialize_writ(result.writ)
    if call:
        assert signed.payload().operation == call.to_operation()
        assert (
            signed.payload().operation.arguments_sha256
            == hashlib.sha256(call.canonical_arguments()).hexdigest()
        )
        assert signed.payload().operation.tool_name == call.tool_name
        assert ARG_MARK not in result.writ and MARK_A not in result.writ


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["shell", "propose", "structured"])
async def test_p02_p03_real_ingress_operator_context_reaches_review_despite_forgery(harness, path):
    s = harness.service([definition()])
    forged = dict(
        context=FORGED,
        evidence=[FORGED],
        metadata={"tools": [FORGED]},
        _trusted_tool_context={"tools": [FORGED]},
    )
    if path == "propose":
        request = ProposeIntentInput.model_validate(
            dict(
                agent_id="qa_agent",
                council_id="qa_a",
                description="Explicit authorized scratch work",
                target_resource="/tmp/scratch",
                command="ls /tmp/scratch",
                working_directory="/tmp",
                tool_call=tool_call().model_dump(mode="json"),
                **forged,
            )
        )
        result = await s.propose_intent(request)
        assert result.success
        assert harness.opa_calls[0]["intent"]["operation"] is None
        assert not harness.mint_calls, "Advisory propose minted a writ"
    else:
        result = await s.authorize(
            authorize_request(tool_call() if path == "structured" else None, **forged)
        )
        assert result.status == "approved" and result.writ
    assert harness.backend.calls
    assert_trusted_messages(
        harness.backend.calls[0], [definition()], expect_args=path == "structured"
    )
    assert harness.opa_calls[0]["council"]["tools"] == [definition()]


@pytest.mark.asyncio
async def test_p03_code_review_model_copy_retains_trusted_catalog_for_member():
    i = attach(intent_for(forged=True), context_for())
    file_backend = RecordingBackend()
    review = await reviewer_call("packaged_files", i, file_backend)
    assert review.decision == "approve", review.reasoning
    # This existing static seam is explicitly named in the frozen design.
    copied = CouncilService._intent_with_code_review(i, review)
    assert copied is not i
    assert copied._trusted_tool_context.to_public_dict() == i._trusted_tool_context.to_public_dict()
    member_backend = RecordingBackend()
    await reviewer_call("member", copied, member_backend)
    assert_trusted_messages(file_backend.calls[0], [definition()])
    assert_trusted_messages(member_backend.calls[0], [definition()])


@pytest.mark.asyncio
async def test_p05_invalid_reload_never_serves_permissive_old_service(harness, tmp_path):
    path = tmp_path / "invalid-reload-pipeline_qa.yaml"
    write_catalog(path, [definition()])
    registry = CouncilRegistry(load_council_catalog(str(path)), catalog_path=path)
    held = await registry.get("qa_a")
    write_catalog(path, None)
    with pytest.raises((ValueError, RuntimeError)):
        await registry.get("qa_a")
    assert not harness.opa_calls and not harness.reviewer_calls and not harness.mint_calls
    # A held valid acquisition remains usable despite invalid future config.
    result = await held.authorize(authorize_request(tool_call()))
    assert result.status == "approved"


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["", "v2"])
async def test_p08_catalog_invalid_call_denied_through_registry_routing(harness, tmp_path, version):
    path = tmp_path / "routing-pipeline_qa.yaml"
    write_catalog(path, [definition()])
    registry = CouncilRegistry(load_council_catalog(str(path)), catalog_path=path)
    request = authorize_request(tool_call(args=arguments(limit=6)), api_version=version)
    service, routed = await registry.service_for_authorize(request)
    result = await service.authorize(routed)
    assert not harness.opa_calls and not harness.reviewer_calls and not harness.mint_calls
    assert result.status == "error" and result.writ is None
    assert result.error.startswith("Invalid tool call:")


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["", "v1", "v2"])
async def test_p08_control_legacy_shell_version_aliases_use_same_service(harness, version):
    result = await harness.service().authorize(authorize_request(api_version=version))
    assert result.status == "approved" and result.writ
    assert harness.opa_calls and harness.reviewer_calls and harness.mint_calls


@pytest.mark.asyncio
async def test_p04_schema_integer_float_keeps_exact_business_bytes(harness):
    call = tool_call(args=arguments(limit=2.0))
    result = await harness.service([definition()]).authorize(authorize_request(call))
    assert result.status == "approved" and result.writ
    value = harness.opa_calls[0]["intent"]["tool_arguments"]["limit"]
    assert type(value) is float and value == 2.0
    signed = WritFactory.deserialize_writ(result.writ)
    assert (
        signed.payload().operation.arguments_sha256
        == hashlib.sha256(call.canonical_arguments()).hexdigest()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["Authorize", "CallTool"])
@pytest.mark.parametrize("encoding", ["raw_json", "struct"])
@pytest.mark.parametrize("declared", [True, False], ids=["catalog-deny", "legacy-control"])
async def test_p08_grpc_forwards_to_same_authorization_service(harness, entry, encoding, declared):
    from google.protobuf.json_format import ParseDict
    from google.protobuf.struct_pb2 import Struct

    from nomos.grpc.mcp_client import MCPToolResult
    from nomos.grpc.server import CouncilGrpcProxy
    from nomos.grpc.v1.council_pb2 import CallToolRequest, CouncilToolRequest

    service = harness.service([definition()] if declared else OMITTED)
    call = tool_call(args=arguments(limit=6) if declared else arguments())
    raw = authorize_request(call).model_dump(mode="json")
    forwarded = []

    class InProcessClient:
        async def call_tool(self, tool_name, data):
            forwarded.append((tool_name, copy.deepcopy(data)))
            assert tool_name == "council_authorize"
            result = await service.authorize(AuthorizeInput.model_validate(data))
            payload = result.model_dump(mode="json")
            return MCPToolResult(data=payload, raw_json=json.dumps(payload))

    class Context:
        async def abort(self, code, details):
            raise AssertionError(f"Unexpected gRPC abort: {code}: {details}")

    request_type = CouncilToolRequest if entry == "Authorize" else CallToolRequest
    kwargs = {"tool_name": "council_authorize"} if entry == "CallTool" else {}
    if encoding == "raw_json":
        kwargs["raw_json"] = json.dumps(raw)
    else:
        # Protobuf Struct numbers are doubles; omit the optional version so the
        # existing strict integer version default applies (unchanged base behavior).
        raw["tool_call"].pop("schema_version")
        kwargs["arguments"] = ParseDict(raw, Struct())
    proxy = CouncilGrpcProxy(InProcessClient(), "http://unused.invalid/mcp")
    response = await getattr(proxy, entry)(request_type(**kwargs), Context())
    assert len(forwarded) == 1
    result = json.loads(response.raw_json)
    if declared:
        assert not harness.opa_calls and not harness.reviewer_calls and not harness.mint_calls
        assert result["status"] == "error" and result["writ"] is None
        assert result["error"].startswith("Invalid tool call:")
    else:
        assert harness.opa_calls and harness.reviewer_calls and harness.mint_calls
        assert result["status"] == "approved" and result["writ"]
