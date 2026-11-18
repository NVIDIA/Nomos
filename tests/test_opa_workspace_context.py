# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Workspace projection is explicit, profile-independent and review-only."""

import pytest
from pydantic import ValidationError

from nomos.config.settings import OPAConfig
from nomos.level_0.types import Intent, ToolCallRequest
from nomos.nat.functions import AuthorizeInput
from nomos.opa import OPAPolicyDecision, build_intent_filter_input
from tests.test_opa_nat_integration import RecordingOPAClient, _make_service


def _intent(arguments, tool_name="shell.run"):
    material = ToolCallRequest(tool_name=tool_name, arguments=arguments, subject="user-1")
    intent = Intent.create(
        agent_id="agent-1",
        description="Review a workspace operation",
        target_resource=tool_name,
        command_type="api",
        operation=material.to_operation(),
    )
    intent._tool_review_material = material
    return intent


def _input(intent, config):
    return build_intent_filter_input(
        intent, request_type="authorize", authenticated=True, opa_config=config
    )


@pytest.mark.parametrize("profile", ["strict-shell", "read-only-relaxed", "local-bash"])
@pytest.mark.parametrize(
    ("tool", "key"), [("shell.run", "project_root"), ("file.write", "scope.root")]
)
def test_workspace_uses_exact_binding_without_changing_arguments_or_intent(profile, tool, key):
    """A model-facing workspace spelling cannot replace the configured argument."""
    intent = _intent(
        {
            "command": "echo hello",
            key: "/projects/demo",
            "workspace": "/",
            "cwd": "/projects/demo/src",
        },
        tool,
    )
    before = intent.model_dump(mode="json")
    config = OPAConfig(
        policy_id=profile,
        shell_tool_arguments={tool: "command"} if profile == "local-bash" else {},
        workspace_arguments={tool: key},
    )
    result = _input(intent, config)["intent"]
    assert result["workspace"] == "/projects/demo"
    assert result["tool_arguments"] == intent._tool_review_material.arguments
    assert result["tool_arguments"]["workspace"] == "/"
    assert result["tool_arguments"]["cwd"] == "/projects/demo/src"
    assert result["working_directory"] == ""
    assert intent.model_dump(mode="json") == before
    assert "workspace" not in before
    assert ("shell_analysis" in result) is (profile == "local-bash")


@pytest.mark.parametrize(
    "root",
    ["/", "/projects/demo", "/projects/demo/../other", r"C:\Projects\demo", r"\\server\share\demo"],
)
def test_absolute_workspace_paths_are_preserved_without_filesystem_resolution(root):
    result = _input(_intent({"root": root}), OPAConfig(workspace_arguments={"shell.run": "root"}))
    assert result["intent"]["workspace"] == root


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "relative/root",
        "~/project",
        "$PROJECT",
        " /project",
        "/project ",
        "/bad\x00root",
        "/bad\nroot",
        True,
        12,
        [],
        {},
    ],
)
def test_invalid_workspace_is_explicitly_unknown_not_a_cwd_fallback(value):
    result = _input(
        _intent({"root": value, "cwd": "/projects/demo", "workspace": "/forged"}),
        OPAConfig(workspace_arguments={"shell.run": "root"}),
    )
    assert "workspace" in result["intent"]
    assert result["intent"]["workspace"] is None


def test_missing_mapped_argument_is_explicitly_unknown():
    result = _input(_intent({}), OPAConfig(workspace_arguments={"shell.run": "root"}))
    assert result["intent"]["workspace"] is None


@pytest.mark.parametrize("bindings", [{}, {"other.tool": "root"}, {"shell.*": "root"}])
def test_unconfigured_tools_do_not_acquire_workspace_context(bindings):
    assert OPAConfig().workspace_arguments == {}
    intent = _intent({"root": "/projects/demo", "workspace": "/forged"})
    result = _input(intent, OPAConfig(workspace_arguments=bindings))
    assert result == _input(intent, OPAConfig())
    assert "workspace" not in result["intent"]


def test_legacy_shell_input_does_not_infer_a_workspace_from_cwd_or_description():
    intent = Intent.create(
        agent_id="agent-1",
        command="/bin/echo hello",
        working_directory="/projects/demo",
        target_resource="/projects/demo",
        description="Work inside /projects/demo",
    )
    result = _input(intent, OPAConfig(workspace_arguments={"shell.run": "root"}))
    assert result == _input(intent, OPAConfig())
    assert "workspace" not in result["intent"]


@pytest.mark.parametrize(
    "bindings",
    [
        {"": "root"},
        {"shell.run": ""},
        {"shell.run ": "root"},
        {"shell.run": " root"},
        {"shell.run": "root\n"},
    ],
)
def test_workspace_configuration_rejects_ambiguous_keys(bindings):
    with pytest.raises(ValidationError, match="workspace tool names"):
        OPAConfig(workspace_arguments=bindings)


@pytest.mark.asyncio
async def test_policy_only_authorization_receives_workspace_without_minting_a_writ(tmp_path):
    """Exercise the service handoff, not just the input-builder helper."""
    service = _make_service(tmp_path)
    service._settings.opa.workspace_arguments = {"shell.run": "project_root"}
    opa = RecordingOPAClient(OPAPolicyDecision(allow=True))
    service._opa_client = opa
    result = await service.authorize(
        AuthorizeInput(
            agent_id="agent-1",
            council_id=service.council_id,
            description="Review the selected workspace",
            justification="Exercise structured policy inspection",
            policy_only=True,
            tool_call=ToolCallRequest(
                tool_name="shell.run",
                subject="user-1",
                arguments={"command": "echo hello", "project_root": "/projects/demo"},
            ),
        )
    )
    assert result.status == "policy_allowed", result.error
    assert result.writ is None
    assert len(opa.calls) == 1
    assert opa.calls[0]["intent"]["workspace"] == "/projects/demo"
    assert opa.calls[0]["request"]["type"] == "policy_only"
