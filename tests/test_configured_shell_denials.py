# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Operator-owned exact executable prohibitions cannot come from tool arguments."""

import pytest
from pydantic import ValidationError

from nomos.config.settings import OPAConfig
from nomos.level_0.types import Intent, ToolCallRequest
from nomos.opa import build_intent_filter_input


def test_command_prohibitions_default_to_separate_empty_lists():
    first, second = OPAConfig(), OPAConfig()
    assert first.denied_shell_commands == second.denied_shell_commands == []
    first.denied_shell_commands.append("synthetic-blocked")
    assert second.denied_shell_commands == []


def test_prohibitions_are_sorted_deduplicated_and_case_sensitive():
    supplied = ["zeta-tool", "demo-tool", "Demo-tool", "demo-tool"]
    config = OPAConfig(denied_shell_commands=supplied)
    assert config.denied_shell_commands == ["Demo-tool", "demo-tool", "zeta-tool"]
    # Normalization must not change the caller's list or retain an alias to it.
    assert supplied == ["zeta-tool", "demo-tool", "Demo-tool", "demo-tool"]
    supplied.append("another-tool")
    assert "another-tool" not in config.denied_shell_commands


@pytest.mark.parametrize(
    "name",
    [
        "",
        " ",
        " leading",
        "trailing ",
        "two words",
        "/opt/tools/demo",
        "./demo",
        "build/demo",
        r"build\demo",
        "tab\tname",
        "newline\nname",
        "nul\x00name",
        "delete\x7fname",
        "c1\x80name",
        "unicode\u2003space",
        "*",
        "demo*",
        "demo?",
        "demo[ab]",
        "demo[",
        "demo]",
    ],
)
def test_prohibitions_reject_paths_patterns_and_ambiguous_names(name):
    with pytest.raises(ValidationError):
        OPAConfig(denied_shell_commands=[name])


def _intent(*, arguments=None):
    if arguments is None:
        return Intent.create(
            agent_id="test-agent",
            description="Inspect an exact shell command",
            target_resource="stdout",
            command="/bin/echo hello",
            working_directory="/workspace/project",
        )
    material = ToolCallRequest(tool_name="shell.run", arguments=arguments, subject="test-user")
    intent = Intent.create(
        agent_id="test-agent",
        description="Inspect an exact application command",
        target_resource="stdout",
        command_type="api",
        operation=material.to_operation(),
    )
    intent._tool_review_material = material
    return intent


@pytest.mark.parametrize("structured", [False, True])
def test_both_input_shapes_copy_operator_prohibitions(structured):
    config = OPAConfig(denied_shell_commands=["zeta-tool", "demo-tool"])
    intent = _intent(arguments={"command": "echo hello"} if structured else None)
    document = build_intent_filter_input(
        intent, request_type="authorize", authenticated=True, opa_config=config
    )
    assert document["council"]["opa_denied_shell_commands"] == ["demo-tool", "zeta-tool"]
    document["council"]["opa_denied_shell_commands"].clear()
    assert config.denied_shell_commands == ["demo-tool", "zeta-tool"]
    assert build_intent_filter_input(
        intent, request_type="authorize", authenticated=True, opa_config=config
    )["council"]["opa_denied_shell_commands"] == ["demo-tool", "zeta-tool"]


def test_default_input_contains_no_operator_prohibitions():
    document = build_intent_filter_input(_intent(), request_type="authorize", authenticated=True)
    assert document["council"]["opa_denied_shell_commands"] == []


def test_tool_arguments_cannot_replace_council_owned_prohibitions():
    arguments = {
        "command": "demo-tool --inspect",
        "denied_shell_commands": [],
        "opa_denied_shell_commands": [],
        "council": {"opa_denied_shell_commands": ["different-tool"]},
    }
    intent = _intent(arguments=arguments)
    config = OPAConfig(
        policy_id="local-bash",
        shell_tool_arguments={"shell.run": "command"},
        denied_shell_commands=["demo-tool"],
    )
    document = build_intent_filter_input(
        intent, request_type="authorize", authenticated=True, opa_config=config
    )
    assert document["intent"]["tool_arguments"] == arguments
    assert document["council"]["opa_denied_shell_commands"] == ["demo-tool"]
    document["intent"]["tool_arguments"]["council"]["opa_denied_shell_commands"].append(
        "another-tool"
    )
    assert document["council"]["opa_denied_shell_commands"] == ["demo-tool"]
    assert config.denied_shell_commands == ["demo-tool"]
    assert intent._tool_review_material.arguments == arguments
