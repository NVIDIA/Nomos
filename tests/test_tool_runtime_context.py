# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Host resolution trust, review visibility and unchanged operation binding."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from nomos.auth.authenticator import AgentIdentity
from nomos.config.settings import CouncilSettings
from nomos.level_0.types import CouncilMember, Intent
from nomos.level_0.types.runtime_context import ToolRuntimeContext
from nomos.level_0.types.tool_call import ToolCallRequest
from nomos.llm.council_agent import IntentEvaluation, _format_intent_for_review
from nomos.nat.functions import AuthorizeInput, CouncilService
from nomos.opa.client import build_intent_filter_input


def context(**overrides):
    return {
        "version": 1,
        "source": "example.application",
        "runtime": {
            "working_directory": "/host/projects/demo",
            "workspace_mappings": [
                {"logical_root": "/workspace/demo", "execution_root": "/host/projects/demo"}
            ],
        },
        **overrides,
    }


def request(**overrides):
    return AuthorizeInput(
        agent_id="untrusted-payload-name",
        council_id="test",
        description="Create one requested directory",
        target_resource="/workspace/demo/output",
        justification="Requested by the user for this test",
        tool_call={
            "tool_name": "files.run",
            "subject": "user",
            "arguments": {"script": "mkdir output"},
            "runtime_context": context(**overrides),
        },
    )


def service(tmp_path, *, trusted=True):
    settings = CouncilSettings(
        council_id="test",
        require_auth=False,
        members=[],
        council_key_path=str(tmp_path / "keys"),
        opa={"enabled": False},
        runtime_context={
            "trusted_hosts": (
                [
                    {
                        "agent_id": "host-client",
                        "source": "example.application",
                        "tools": ["files.run"],
                    }
                ]
                if trusted
                else []
            )
        },
    )
    return CouncilService(settings)


def identity(name="host-client"):
    return AgentIdentity(
        agent_id=name, display_name="Test host", permissions={"tool:write"}, councils={"test"}
    )


@pytest.mark.parametrize("version", [True, 1.0, "1", 2])
def test_runtime_version_is_strict(version):
    with pytest.raises(ValidationError):
        ToolRuntimeContext.model_validate(context(version=version))


@pytest.mark.parametrize(
    "path", ["relative", "/host/../other", "/host/./demo", "/host/\nmalformed", " /host/demo"]
)
def test_runtime_paths_are_absolute_unambiguous_data(path):
    data = context()
    data["runtime"]["working_directory"] = path
    with pytest.raises(ValidationError):
        ToolRuntimeContext.model_validate(data)


def test_remote_windows_paths_are_not_resolved_on_this_service():
    data = context()
    data["runtime"]["working_directory"] = r"C:\projects\demo"
    assert ToolRuntimeContext.model_validate(data).runtime.working_directory == r"C:\projects\demo"


@pytest.mark.parametrize(
    "who,trusted,source",
    [
        (None, True, "example.application"),
        ("other", True, "example.application"),
        ("host-client", False, "example.application"),
        ("host-client", True, "other.application"),
    ],
)
def test_trust_uses_authenticated_identity_source_and_exact_tool(tmp_path, who, trusted, source):
    svc = service(tmp_path, trusted=trusted)
    accepted, error = svc._trusted_runtime_context(
        request(source=source), identity(who) if who else None
    )
    assert accepted is None
    assert error


def test_accepted_context_is_snapshotted_and_does_not_change_writ_operation(tmp_path):
    req = request()
    svc = service(tmp_path)
    accepted, error = svc._trusted_runtime_context(req, identity())
    assert error is None
    original = req.tool_call.to_operation()
    req.tool_call.runtime_context.runtime.workspace_mappings.clear()
    assert len(accepted.runtime.workspace_mappings) == 1
    without = ToolCallRequest.model_validate(
        {"tool_name": "files.run", "subject": "user", "arguments": {"script": "mkdir output"}}
    )
    assert without.to_operation() == original


def test_mutated_prevalidated_runtime_context_is_revalidated_at_admission(tmp_path):
    req = request()
    mappings = req.tool_call.runtime_context.runtime.workspace_mappings
    mappings.extend(mappings * 17)
    accepted, error = service(tmp_path)._trusted_runtime_context(req, identity())
    assert accepted is None and error


@pytest.mark.parametrize(
    "second",
    [
        "/workspace/demo/",
        "/workspace//demo",
        "//workspace/demo",
        "///workspace/demo",
        "/workspace/demo/sub",
    ],
)
def test_ambiguous_and_overlapping_roots_are_not_accepted(second):
    data = context()
    data["runtime"]["workspace_mappings"].append(
        {"logical_root": second, "execution_root": "/other"}
    )
    with pytest.raises(ValidationError, match="duplicate or overlap"):
        ToolRuntimeContext.model_validate(data)


@pytest.mark.parametrize(
    "first,second",
    [
        (r"C:\Projects\Demo", "c:/projects/demo/"),
        (r"C:\Projects\Demo", r"c:\projects\demo\src"),
        ("//server/share", r"\\SERVER\SHARE"),
    ],
)
def test_remote_windows_equivalent_roots_cannot_hide_conflicting_mappings(first, second):
    data = context()
    data["runtime"]["workspace_mappings"] = [
        {"logical_root": first, "execution_root": "/host/first"},
        {"logical_root": second, "execution_root": "/host/second"},
    ]
    with pytest.raises(ValidationError, match="duplicate or overlap"):
        ToolRuntimeContext.model_validate(data)


@pytest.mark.parametrize(
    "first,second",
    [
        ("/workspace/demo", "/workspace/demo-other"),
        (r"C:\Projects\Demo", "c:/projects/demo-other"),
        ("//server/share", "//server/share-other"),
    ],
)
def test_distinct_path_components_are_not_treated_as_overlapping_roots(first, second):
    data = context()
    data["runtime"]["workspace_mappings"] = [
        {"logical_root": first, "execution_root": "/host/first"},
        {"logical_root": second, "execution_root": "/host/second"},
    ]
    accepted = ToolRuntimeContext.model_validate(data)
    assert [mapping.logical_root for mapping in accepted.runtime.workspace_mappings] == [
        first,
        second,
    ]


def test_only_admitted_live_context_reaches_review_and_opa(tmp_path):
    req = request()
    intent = Intent.create(
        agent_id="host-client",
        description=req.description,
        target_resource=req.target_resource,
        operation=req.tool_call.to_operation(),
    )
    intent._tool_review_material = req.tool_call
    assert "Operator-Trusted Host Runtime" not in _format_intent_for_review(intent)
    accepted, error = service(tmp_path)._trusted_runtime_context(req, identity())
    assert error is None
    intent._trusted_runtime_context = accepted
    prompt = _format_intent_for_review(intent)
    assert "Operator-Trusted Host Runtime Resolution" in prompt
    assert '"execution_root":"/host/projects/demo"' in prompt
    assert "not proof of confinement" in prompt
    assert "/host/projects/demo" not in str(intent.model_dump())
    opa = build_intent_filter_input(intent, request_type="authorize", authenticated=True)
    assert opa["intent"]["working_directory"] == "/host/projects/demo"
    assert opa["intent"]["runtime_context"]["source"] == "example.application"


@pytest.mark.asyncio
async def test_untrusted_context_stops_before_opa_or_members(tmp_path, monkeypatch):
    svc = service(tmp_path, trusted=False)
    monkeypatch.setattr(
        svc, "_resolve_pre_intent_agent", AsyncMock(return_value=(identity(), None))
    )
    opa = AsyncMock()
    monkeypatch.setattr(svc, "_evaluate_opa_filter", opa)
    result = await svc.authorize(request())
    assert result.status == "error"
    assert result.error_code == "untrusted_runtime_context"
    opa.assert_not_awaited()


@pytest.mark.asyncio
async def test_trusted_context_enters_normal_council_review(tmp_path, monkeypatch):
    svc = service(tmp_path)
    monkeypatch.setattr(
        svc, "_resolve_pre_intent_agent", AsyncMock(return_value=(identity(), None))
    )
    reviewed = []

    async def evaluate(intent):
        reviewed.append(intent)
        return IntentEvaluation(decision="approve", reasoning="Scoped test operation")

    svc._agents.append(
        SimpleNamespace(
            member_id="member",
            display_name="Member",
            provider_name="fixture",
            evaluate_intent=evaluate,
        )
    )
    svc.council.register_council_member(
        CouncilMember(member_id="member", display_name="Member", public_key_hex="", weight=1)
    )
    result = await svc.authorize(request())
    assert result.status == "approved", result.error
    assert len(reviewed) == 1
    assert reviewed[0].agent_id == "host-client"
    assert reviewed[0]._trusted_runtime_context.runtime.working_directory == "/host/projects/demo"


def test_decision_model_receives_admitted_runtime_and_refuses_changed_redacted_facts(tmp_path):
    import json

    from nomos.system_one.client import SystemOneEvaluationError, build_state

    req = request()
    intent = Intent.create(
        agent_id="host-client",
        description=req.description,
        target_resource=req.target_resource,
        operation=req.tool_call.to_operation(),
    )
    assert "runtime_context" not in json.loads(build_state(intent, max_chars=10000))
    intent._trusted_runtime_context = service(tmp_path)._trusted_runtime_context(req, identity())[0]
    state = json.loads(build_state(intent, max_chars=10000))
    assert state["runtime_context"]["runtime"]["working_directory"] == "/host/projects/demo"
    assert state["working_directory"] == "/host/projects/demo"
    with pytest.raises(SystemOneEvaluationError, match="over"):
        build_state(intent, max_chars=10)


def test_wrong_exact_tool_cannot_inherit_host_trust(tmp_path):
    req = request()
    req.tool_call = req.tool_call.model_copy(update={"tool_name": "files.other"})
    admitted, error = service(tmp_path)._trusted_runtime_context(req, identity())
    assert admitted is None and error
