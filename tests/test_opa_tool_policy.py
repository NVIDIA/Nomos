# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Structured policy inputs and fail-closed, inspection-only authorization."""

import json
import os
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from nomos.config.settings import OPAConfig
from nomos.level_0.types import Intent, ToolCallRequest
from nomos.level_1.ledger.entry import LedgerEntryType
from nomos.nat.functions import AuthorizeInput
from nomos.opa import (
    OPAEvaluationError,
    OPAPolicyDecision,
    build_intent_filter_input,
    parse_opa_decision,
)
from nomos.opa.shell_analysis import analyze_shell_command
from tests.test_opa_nat_integration import RecordingOPAClient, _make_service


@pytest.mark.parametrize(
    "command",
    [
        "echo hello",
        "/bin/rm -rf directory",
        "sudo id",
        "su -lp root",
        "chmod 777 file",
        "rm -- -rf",
        "git status",
        "python3 script.py",
        "python -c 'pass'",
        "bash -lc 'id'",
        "/tmp/echo hi",
    ],
)
def test_literal_command_analysis(command):
    assert analyze_shell_command(command)["supported"] is True


@pytest.mark.parametrize(
    "command",
    [
        None,
        "",
        "echo 'unterminated",
        "echo $(id)",
        "echo `id`",
        "echo hi; sudo id",
        "cat file | head",
        "rm *",
        "X=1 echo hi",
        "echo hi > out",
        "echo hi\nid",
        "echo hi &",
        "echo hi\x00",
        "a" * 16385,
    ],
)
def test_opaque_or_unsupported_commands_require_interpretation(command):
    assert analyze_shell_command(command)["supported"] is False


def test_profile_binding_is_explicit_and_empty_profile_is_rejected():
    assert OPAConfig().shell_tool_arguments == {}
    with pytest.raises(ValidationError):
        OPAConfig(policy_id="local-bash")
    with pytest.raises(ValidationError):
        OPAConfig(shell_tool_arguments={"bash": ""})


def test_actual_arguments_are_live_opa_input_and_do_not_change_durable_binding():
    material = ToolCallRequest(
        tool_name="shell.run",
        arguments={"command": "rm -rf dir", "nested": {"x": 1}},
        subject="user-1",
    )
    intent = Intent.create(
        agent_id="agent-1",
        description="Run a local command",
        target_resource="shell.run",
        command_type="api",
        operation=material.to_operation(),
    )
    intent._tool_review_material = material
    config = OPAConfig(policy_id="local-bash", shell_tool_arguments={"shell.run": "command"})
    result = build_intent_filter_input(
        intent, request_type="authorize", authenticated=True, opa_config=config
    )
    assert result["intent"]["tool_arguments"] == material.arguments
    assert result["intent"]["shell_analysis"]["commands"] == [
        {"executable": "rm", "name": "rm", "argv": ["-rf", "dir"]}
    ]
    result["intent"]["tool_arguments"]["nested"]["x"] = 2
    assert material.arguments["nested"]["x"] == 1
    assert "arguments" not in intent.model_dump(mode="json")["operation"]
    assert "tool_arguments" not in intent.model_dump(mode="json")
    ordinary = build_intent_filter_input(intent, request_type="authorize", authenticated=True)
    assert ordinary["intent"]["tool_arguments"] == material.arguments
    assert "shell_analysis" not in ordinary["intent"]


@pytest.mark.parametrize(
    ("allow", "decision"),
    [(True, "forbidden"), (True, "requires_human_approval"), (False, "allow"), (False, "unknown")],
)
def test_contradictory_policy_decisions_fail_closed(allow, decision):
    with pytest.raises(OPAEvaluationError):
        parse_opa_decision({"result": {"allow": allow, "policy_decision": decision}})


def _request(**kwargs):
    return AuthorizeInput(
        agent_id="agent-1", command="/bin/echo hello", working_directory="/tmp", **kwargs
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["forbidden", "requires_human_approval"])
@pytest.mark.parametrize("policy_only", [True, False])
async def test_policy_blocks_before_models_and_never_mints_writ(
    tmp_path, monkeypatch, decision, policy_only
):
    service = _make_service(tmp_path)
    service._opa_client = RecordingOPAClient(
        OPAPolicyDecision(allow=False, policy_decision=decision, reasons=["Policy reason"])
    )

    async def unexpected(*args, **kwargs):
        pytest.fail("Model review must not run")

    monkeypatch.setattr(service, "_run_code_review", unexpected)
    result = await service.authorize(_request(policy_only=policy_only))
    assert result.status == "denied"
    assert result.policy_decision == decision
    assert result.writ is None
    assert service.council.get_workflow(result.decision_id) is None


@pytest.mark.asyncio
async def test_policy_only_allow_is_not_an_execution_authorization(tmp_path, monkeypatch):
    service = _make_service(tmp_path)
    service._opa_client = RecordingOPAClient(OPAPolicyDecision(allow=True))

    async def unexpected(*args, **kwargs):
        pytest.fail("Model review must not run")

    monkeypatch.setattr(service, "_run_code_review", unexpected)
    result = await service.authorize(_request(policy_only=True))
    assert result.status == "policy_allowed"
    assert result.policy_decision == "allow"
    assert result.writ is None
    assert service.council.get_workflow(result.decision_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["disabled", "undefined", "failure"])
@pytest.mark.parametrize("fail_closed", [True, False])
async def test_policy_only_never_accepts_disabled_undefined_or_unavailable_opa(
    tmp_path, condition, fail_closed
):
    service = _make_service(tmp_path, fail_closed=fail_closed)
    if condition == "disabled":
        service._opa_client = None
    elif condition == "undefined":
        service._opa_client = RecordingOPAClient(parse_opa_decision({}))
    else:
        service._opa_client = RecordingOPAClient(error=OPAEvaluationError("unavailable"))
    result = await service.authorize(_request(policy_only=True))
    assert result.status == "error"
    assert result.policy_decision is None
    assert result.writ is None


def test_policy_only_requires_a_boolean():
    with pytest.raises(ValidationError):
        _request(policy_only="false")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy_only", "decision"),
    [
        (True, "allow"),
        (True, "forbidden"),
        (True, "requires_human_approval"),
        (False, "forbidden"),
        (False, "requires_human_approval"),
    ],
)
async def test_policy_outcomes_record_verdict_without_actual_arguments_in_legacy_audit_mode(
    tmp_path, policy_only, decision
):
    service = _make_service(tmp_path)
    service.council.config.consolidated_review_audit = False
    service._settings.opa = OPAConfig(
        policy_id="local-bash", shell_tool_arguments={"shell.run": "command"}
    )
    service._opa_client = RecordingOPAClient(
        OPAPolicyDecision(allow=decision == "allow", policy_decision=decision)
    )
    request = AuthorizeInput(
        agent_id="agent-1",
        council_id="opa-test-council",
        policy_only=policy_only,
        tool_call=ToolCallRequest(
            tool_name="shell.run",
            subject="user-1",
            arguments={"command": "echo private-argument-marker"},
        ),
    )
    result = await service.authorize(request)
    assert result.writ is None
    ledger = service.council.ledger
    reviews = [
        entry
        for i in range(ledger.length())
        if (entry := ledger.get(i)).entry_type == LedgerEntryType.INTENT_REVIEW_RECORDED
    ]
    assert len(reviews) == 1
    assert reviews[0].payload["request_type"] == ("policy_only" if policy_only else "authorize")
    assert reviews[0].payload["opa"]["policy_decision"] == decision
    assert reviews[0].payload["chairman"] == {}
    assert reviews[0].payload["quorum"] == {}
    assert "private-argument-marker" not in json.dumps(reviews[0].payload)


@pytest.mark.asyncio
async def test_normal_fail_closed_error_does_not_claim_a_policy_prohibition(tmp_path):
    service = _make_service(tmp_path)
    service._opa_client = RecordingOPAClient(error=OPAEvaluationError("unavailable"))
    result = await service.authorize(_request())
    assert result.status == "denied"  # Preserve the existing fail-closed API.
    assert result.policy_decision is None


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("echo hello", "allow"),
        ("echo 'sudo rm -rf directory'", "allow"),
        ("/bin/mkdir demo", "allow"),
        ("git status", "allow"),
        ("python3 script.py", "allow"),
        ("npm test", "allow"),
        ("/workspace/project/build/test_runner --quick", "allow"),
        ("./build/test_runner --quick", "allow"),
        ("'/workspace/build tools/test runner' --quick", "allow"),
        ("/usr/local/bin/custom-tool hello", "allow"),
        ("/opt/tools/echo hello", "allow"),
        ("rm -rf directory", "forbidden"),
        ("/bin/rm -fr directory", "forbidden"),
        ("/workspace/build/rm -rf output", "forbidden"),
        ("./build/rm -rf output", "forbidden"),
        ("'./build tools/rm' -rf output", "forbidden"),
        ("./build/rm -- -rf", "requires_human_approval"),
        ("rm -rf output " + "a " * 5000, "forbidden"),
        ("sudo " + "a" * 16400, "forbidden"),
        ("sudo id 'unterminated", "forbidden"),
        ("rm -r -f directory", "forbidden"),
        ("rm -rf *", "forbidden"),
        ("rm -rf /tmp/*", "forbidden"),
        ('rm -rf "$TARGET"', "forbidden"),
        ('rm "$OPTIONS" -rf target', "requires_human_approval"),
        ("echo 'rm -rf *'", "requires_human_approval"),
        ("rm --recursive --force directory", "forbidden"),
        ("rm -R --force directory", "forbidden"),
        ("rm -- -rf", "requires_human_approval"),
        ("rm file", "requires_human_approval"),
        ("sudo id", "forbidden"),
        ("/usr/bin/sudo id", "forbidden"),
        ("/custom/tools/sudo id", "forbidden"),
        ("su - root", "forbidden"),
        ("su -lc id", "forbidden"),
        ("/workspace/build/su -lc id", "forbidden"),
        ("su --login root", "forbidden"),
        ("su root", "requires_human_approval"),
        ("chmod 777 file", "forbidden"),
        ("chmod -R 0777 directory", "forbidden"),
        ("./build/chmod -R 0777 directory", "forbidden"),
        ("chmod 644 777", "allow"),
        ("bash -lc 'rm -rf directory'", "requires_human_approval"),
        ("/opt/tools/bash -lc 'id'", "requires_human_approval"),
        ("bash --command=id", "requires_human_approval"),
        ("bash -- -c", "allow"),
        ("echo --command=id", "allow"),
        ("exec /bin/rm -rf output", "requires_human_approval"),
        ("command /bin/rm -rf output", "requires_human_approval"),
        ("env /bin/rm -rf output", "requires_human_approval"),
        ("/usr/bin/time /bin/rm -rf output", "requires_human_approval"),
        ("VAR=x sudo id", "requires_human_approval"),
        ("VAR+=x sudo id", "requires_human_approval"),
        ("! sudo id", "requires_human_approval"),
        ("time sudo id", "requires_human_approval"),
        ("rm *", "requires_human_approval"),
        ("echo hi; sudo id", "requires_human_approval"),
        ("echo $(id)", "requires_human_approval"),
    ],
)
def test_real_opa_consumes_actual_command_analysis(command, expected):
    response = _evaluate_real_shell_policy(command)
    assert response["policy_decision"] == expected
    assert response["allow"] == (expected == "allow")


@pytest.mark.parametrize(
    ("command", "denied", "expected"),
    [
        ("restricted-helper task", ["restricted-helper"], "forbidden"),
        ("/workspace/build/restricted-helper task", ["restricted-helper"], "forbidden"),
        ("./build/restricted-helper task", ["restricted-helper"], "forbidden"),
        ("restricted-helper task", [], "allow"),
        ("Restricted-helper task", ["restricted-helper"], "allow"),
        ("echo restricted-helper", ["restricted-helper"], "allow"),
        ("su user", ["su"], "forbidden"),
        ("nohup task", ["nohup"], "forbidden"),
        ("restricted-helper $TARGET", ["restricted-helper"], "forbidden"),
        ("restricted-helper 'unterminated", ["restricted-helper"], "forbidden"),
        ("git status", ["restricted-helper"], "allow"),
    ],
)
def test_real_opa_configured_prohibitions_precede_review(command, denied, expected):
    response = _evaluate_real_shell_policy(command, denied)
    assert response["policy_decision"] == expected
    assert response["allow"] == (expected == "allow")
    if expected == "forbidden":
        assert any("configured shell policy" in reason for reason in response["reasons"])


def _evaluate_real_shell_policy(command, denied_commands=()):
    binary = os.environ.get("NOMOS_TEST_OPA_BINARY")
    if not binary:
        pytest.skip("Set NOMOS_TEST_OPA_BINARY to run the actual Rego engine")
    material = ToolCallRequest(
        tool_name="shell.run", arguments={"command": command}, subject="user-1"
    )
    intent = Intent.create(
        agent_id="agent-1",
        description="Run a local command",
        target_resource="shell.run",
        command_type="api",
        operation=material.to_operation(),
    )
    intent._tool_review_material = material
    config = OPAConfig(
        policy_id="local-bash",
        shell_tool_arguments={"shell.run": "command"},
        denied_shell_commands=list(denied_commands),
    )
    document = build_intent_filter_input(
        intent, request_type="authorize", authenticated=True, opa_config=config
    )
    policy = Path(__file__).resolve().parents[1] / "policies" / "intent_filter.rego"
    completed = subprocess.run(
        [
            binary,
            "eval",
            "--format=json",
            "--data",
            str(policy),
            "--stdin-input",
            "data.nomos.intent.decision",
        ],
        input=json.dumps(document),
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    return json.loads(completed.stdout)["result"][0]["expressions"][0]["value"]
