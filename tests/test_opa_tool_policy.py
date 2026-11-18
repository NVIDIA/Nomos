# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Structured policy inputs and fail-closed, inspection-only authorization."""

import asyncio
import json
import os
import subprocess
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError

from nomos.config.settings import OPAConfig
from nomos.level_0.types import Intent, ToolCallRequest
from nomos.level_1.ledger.entry import LedgerEntryType
from nomos.level_1.ledger.storage import FileLedgerStorage, InMemoryLedgerStorage
from nomos.nat.functions import AuthorizeInput, ProposeIntentInput
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
    assert analyze_shell_command(command)["fully_parsed"] is True


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
def test_opaque_or_unsupported_syntax_is_not_fully_parsed(command):
    assert analyze_shell_command(command)["fully_parsed"] is False


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
    [(True, "deny"), (True, "requires_human_approval"), (False, "allow"), (False, "unknown")],
)
def test_contradictory_decisions_fail_closed(allow, decision):
    with pytest.raises(OPAEvaluationError):
        parse_opa_decision({"result": {"allow": allow, "decision": decision}})


@pytest.mark.parametrize("decision", ["allow", "deny", "requires_human_approval"])
def test_explicit_opa_decision_needs_no_redundant_boolean(decision):
    parsed = parse_opa_decision({"result": {"decision": decision}})
    assert parsed.decision == decision
    assert parsed.allow is (decision == "allow")
    if decision == "requires_human_approval":
        assert parsed.reasons == ["OPA policy requires human approval"]


@pytest.mark.parametrize("decision", ["allow", "forbidden", "requires_human_approval"])
def test_obsolete_opa_discriminator_is_rejected(decision):
    with pytest.raises(OPAEvaluationError, match="obsolete"):
        parse_opa_decision({"result": {"allow": False, "policy_decision": decision}})


@pytest.mark.parametrize("decision", [None, "forbidden", 1, [], {}])
def test_invalid_explicit_opa_decision_is_rejected(decision):
    with pytest.raises(OPAEvaluationError):
        parse_opa_decision({"result": {"allow": False, "decision": decision}})


@pytest.mark.parametrize(
    "result",
    [
        {"allow": True, "allowed": False},
        {"allow": False, "allowed": True},
        {"allow": True, "allowed": 1},
        {"allow": None, "allowed": False},
        {"decision": "allow", "allow": True, "allowed": False},
    ],
)
def test_invalid_or_contradictory_legacy_boolean_aliases_are_rejected(result):
    with pytest.raises(OPAEvaluationError):
        parse_opa_decision({"result": result})


@pytest.mark.parametrize("allow", [True, False])
def test_consistent_legacy_boolean_aliases_are_accepted(allow):
    assert parse_opa_decision({"result": {"allow": allow, "allowed": allow}}).allow is allow


def _request(**kwargs):
    return AuthorizeInput(
        agent_id="agent-1", command="/bin/echo hello", working_directory="/tmp", **kwargs
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["deny", "requires_human_approval"])
@pytest.mark.parametrize("policy_only", [True, False])
async def test_policy_blocks_before_models_and_never_mints_writ(
    tmp_path, monkeypatch, decision, policy_only
):
    service = _make_service(tmp_path)
    service._opa_client = RecordingOPAClient(
        OPAPolicyDecision(allow=False, decision=decision, reasons=["Policy reason"])
    )

    async def unexpected(*args, **kwargs):
        pytest.fail("Model review must not run")

    monkeypatch.setattr(service, "_run_code_review", unexpected)
    result = await service.authorize(_request(policy_only=policy_only))
    assert result.status == (
        "requires_human_approval" if decision == "requires_human_approval" else "denied"
    )
    assert "policy_decision" not in result.model_dump()
    assert result.writ is None
    if decision == "requires_human_approval":
        assert result.error.startswith("Human approval required by OPA policy:")
    else:
        assert result.error.startswith("Denied by OPA policy:")
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
    assert "policy_decision" not in result.model_dump()
    assert result.writ is None
    assert service.council.get_workflow(result.decision_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("storage_kind", ["memory", "file"])
@pytest.mark.parametrize("cancel_review", [False, True])
@pytest.mark.parametrize(
    ("policy_only", "decision"),
    [
        (True, "allow"),
        (True, "deny"),
        (True, "requires_human_approval"),
        (False, "deny"),
        (False, "requires_human_approval"),
    ],
)
async def test_policy_audit_waits_for_inflight_submission(
    tmp_path, monkeypatch, storage_kind, policy_only, decision, cancel_review
):
    """Race an actual policy review against a submission holding an old ledger head."""
    service = _make_service(tmp_path)
    ledger = service.council.ledger
    storage = (
        FileLedgerStorage(tmp_path / "concurrent-ledger.jsonl")
        if storage_kind == "file"
        else InMemoryLedgerStorage()
    )
    # Keep the initialized genesis and bootstrap entries when changing backends.
    for entry in ledger._storage.iterate():
        storage.append(entry)
    monkeypatch.setattr(ledger, "_storage", storage)
    service.council.config.consolidated_review_audit = False
    service._settings.opa = OPAConfig(
        policy_id="local-bash", shell_tool_arguments={"shell.run": "command"}
    )
    submission_paused = asyncio.Event()
    review_reached_opa = asyncio.Event()
    release_submission = threading.Event()
    loop = asyncio.get_running_loop()
    event_loop_thread = threading.get_ident()
    original_build_next = ledger._build_next

    def pause_after_reading_head(head, *args, **kwargs):
        if threading.get_ident() != event_loop_thread and not release_submission.is_set():
            loop.call_soon_threadsafe(submission_paused.set)
            if not release_submission.wait(timeout=10):
                raise TimeoutError("Test did not release the paused submission")
        return original_build_next(head, *args, **kwargs)

    monkeypatch.setattr(ledger, "_build_next", pause_after_reading_head)

    class ConcurrentOPAClient(RecordingOPAClient):
        async def evaluate(self, input_doc):
            if input_doc["request"]["type"] in {"authorize", "policy_only"}:
                review_reached_opa.set()
                return OPAPolicyDecision(allow=decision == "allow", decision=decision)
            return OPAPolicyDecision(allow=True)

    service._opa_client = ConcurrentOPAClient()
    submission = asyncio.create_task(
        service.propose_intent(
            ProposeIntentInput(
                agent_id="agent-1",
                description="Concurrent ordinary submission",
                command="/bin/echo hello",
                working_directory="/tmp",
                target_resource="/tmp",
                justification="Exercise the real single-writer submission path",
            )
        )
    )
    review = None
    try:
        await asyncio.wait_for(submission_paused.wait(), timeout=5)
        review = asyncio.create_task(service.authorize(_request(policy_only=policy_only)))
        await asyncio.wait_for(review_reached_opa.wait(), timeout=5)
        # OPA has completed; the review must wait for the writer that already
        # read the head, rather than append a competing entry with its sequence.
        assert not review.done()
        if cancel_review:
            review.cancel()
    finally:
        release_submission.set()
        tasks = [submission] + ([review] if review is not None else [])
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)

    assert results[0].success
    result = results[1]
    if cancel_review:
        assert isinstance(result, asyncio.CancelledError)
    else:
        expected = {
            "allow": "policy_allowed",
            "deny": "denied",
            "requires_human_approval": "requires_human_approval",
        }[decision]
        assert result.status == expected
    entries = list(storage.iterate())
    assert [entry.sequence for entry in entries] == list(range(len(entries)))
    assert ledger.validate()
    reviews = [e for e in entries if e.entry_type == LedgerEntryType.INTENT_REVIEW_RECORDED]
    assert len(reviews) == 1
    assert reviews[0].payload["opa"]["decision"] == decision
    if not cancel_review:
        assert reviews[0].payload["intent_id"] == result.decision_id
    if storage_kind == "file":
        reloaded = FileLedgerStorage(tmp_path / "concurrent-ledger.jsonl")
        assert [e.content_hash for e in reloaded.iterate()] == [e.content_hash for e in entries]


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
    assert "policy_decision" not in result.model_dump()
    assert result.writ is None


def test_policy_only_requires_a_boolean():
    with pytest.raises(ValidationError):
        _request(policy_only="false")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy_only", "decision"),
    [
        (True, "allow"),
        (True, "deny"),
        (True, "requires_human_approval"),
        (False, "deny"),
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
        OPAPolicyDecision(allow=decision == "allow", decision=decision)
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
    assert reviews[0].payload["opa"]["decision"] == decision
    assert reviews[0].payload["chairman"] == {}
    assert reviews[0].payload["quorum"] == {}
    assert "private-argument-marker" not in json.dumps(reviews[0].payload)


@pytest.mark.asyncio
async def test_normal_fail_closed_error_does_not_claim_a_policy_prohibition(tmp_path):
    service = _make_service(tmp_path)
    service._opa_client = RecordingOPAClient(error=OPAEvaluationError("unavailable"))
    result = await service.authorize(_request())
    assert result.status == "denied"  # Preserve the existing fail-closed API.
    assert "policy_decision" not in result.model_dump()


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("echo hello", "allow"),
        ("echo 'sudo rm -rf directory'", "allow"),
        ("/bin/mkdir demo", "allow"),
        ("git status", "allow"),
        ('cat "$TARGET"', "allow"),
        ("time git status", "allow"),
        ("/usr/bin/time git status", "allow"),
        ("nohup task", "allow"),
        ("python3 script.py", "allow"),
        ("npm test", "allow"),
        ("/workspace/project/build/test_runner --quick", "allow"),
        ("./build/test_runner --quick", "allow"),
        ("'/workspace/build tools/test runner' --quick", "allow"),
        ("/usr/local/bin/custom-tool hello", "allow"),
        ("/opt/tools/echo hello", "allow"),
        ("rm -rf directory", "deny"),
        ("/bin/rm -fr directory", "deny"),
        ("/workspace/build/rm -rf output", "deny"),
        ("./build/rm -rf output", "deny"),
        ("'./build tools/rm' -rf output", "deny"),
        ("./build/rm -- -rf", "requires_human_approval"),
        ("rm -rf output " + "a " * 5000, "deny"),
        ("sudo " + "a" * 16400, "deny"),
        ("sudo id 'unterminated", "deny"),
        ("rm -r -f directory", "deny"),
        ("rm -rf *", "deny"),
        ("rm -rf /tmp/*", "deny"),
        ('rm -rf "$TARGET"', "deny"),
        ('rm "$OPTIONS" -rf target', "requires_human_approval"),
        ("echo 'rm -rf *'", "allow"),
        ("rm --recursive --force directory", "deny"),
        ("rm -R --force directory", "deny"),
        ("rm -- -rf", "requires_human_approval"),
        ("rm file", "requires_human_approval"),
        ("sudo id", "deny"),
        ("/usr/bin/sudo id", "deny"),
        ("/custom/tools/sudo id", "deny"),
        ("su - root", "deny"),
        ("su -lc id", "deny"),
        ("/workspace/build/su -lc id", "deny"),
        ("su --login root", "deny"),
        ("su root", "requires_human_approval"),
        ("chmod 777 file", "deny"),
        ("chmod -R 0777 directory", "deny"),
        ("./build/chmod -R 0777 directory", "deny"),
        ("chmod 644 777", "allow"),
        ("bash -lc 'rm -rf directory'", "allow"),
        ("/opt/tools/bash -lc 'id'", "allow"),
        ("bash --command=id", "allow"),
        ("bash -- -c", "allow"),
        ("echo --command=id", "allow"),
        ("exec /bin/rm -rf output", "allow"),
        ("command /bin/rm -rf output", "allow"),
        ("env /bin/rm -rf output", "allow"),
        ("/usr/bin/time /bin/rm -rf output", "allow"),
        ("VAR=x sudo id", "allow"),
        ("VAR+=x sudo id", "allow"),
        ("! sudo id", "allow"),
        ("time sudo id", "allow"),
        ("rm *", "requires_human_approval"),
        ("echo hi; sudo id", "allow"),
        ("echo $(id)", "allow"),
    ],
)
def test_real_opa_consumes_actual_command_analysis(command, expected):
    response = _evaluate_real_shell_policy(command)
    assert response["decision"] == expected
    assert response["allow"] == (expected == "allow")


@pytest.mark.parametrize(
    ("command", "denied", "expected"),
    [
        ("restricted-helper task", ["restricted-helper"], "deny"),
        ("/workspace/build/restricted-helper task", ["restricted-helper"], "deny"),
        ("./build/restricted-helper task", ["restricted-helper"], "deny"),
        ("restricted-helper task", [], "allow"),
        ("Restricted-helper task", ["restricted-helper"], "allow"),
        ("echo restricted-helper", ["restricted-helper"], "allow"),
        ("su user", ["su"], "deny"),
        ("nohup task", ["nohup"], "deny"),
        ("restricted-helper $TARGET", ["restricted-helper"], "deny"),
        ("restricted-helper 'unterminated", ["restricted-helper"], "deny"),
        ("git status", ["restricted-helper"], "allow"),
    ],
)
def test_real_opa_configured_prohibitions_precede_review(command, denied, expected):
    response = _evaluate_real_shell_policy(command, denied)
    assert response["decision"] == expected
    assert response["allow"] == (expected == "allow")
    if expected == "deny":
        assert any("configured shell policy" in reason for reason in response["reasons"])


@pytest.mark.parametrize("command", ['rm "$TARGET"', 'rm -rf "$TARGET"'])
@pytest.mark.asyncio
async def test_agent_receives_guidance_for_the_final_decision(tmp_path, monkeypatch, command):
    response = _evaluate_real_shell_policy(command)
    decision = parse_opa_decision({"result": response})
    service = _make_service(tmp_path)
    service._opa_client = RecordingOPAClient(decision)

    async def unexpected(*args, **kwargs):
        pytest.fail("Explicit human requirements and denials must not reach Council review")

    monkeypatch.setattr(service, "_run_code_review", unexpected)
    result = await service.authorize(_request())
    assert result.status == (
        "requires_human_approval" if response["decision"] == "requires_human_approval" else "denied"
    )
    assert result.writ is None
    assert "policy_decision" not in result.model_dump()
    # Denials prepend retry-safety advice; human requests retain approval guidance.
    assert set(response["guidance"]).issubset(result.guidance)
    guidance = " ".join(response["guidance"])
    if command.startswith("rm -rf"):
        assert result.status == "denied"
        assert "Do not retry" in guidance
        assert "Submit the intended literal" not in guidance
        assert "or request human approval" not in guidance
    else:
        assert result.status == "requires_human_approval"
        assert result.guidance == response["guidance"]
        assert "requires human approval" in guidance
        assert "Submit the intended literal" not in guidance
        assert "Do not execute before approval" in guidance


@pytest.mark.parametrize(
    "analysis", [{"fully_parsed": False}, {"supported": True}, {"supported": False}, {}]
)
def test_parse_completeness_alone_does_not_require_human_approval(analysis):
    response = _evaluate_real_shell_policy("echo hello", analysis_override=analysis)
    assert response["decision"] == "allow"
    assert response["allow"] is True
    assert response["guidance"] == []


def test_fully_parsed_command_can_still_need_human_approval():
    response = _evaluate_real_shell_policy("rm note.txt")
    assert response["decision"] == "requires_human_approval"
    assert response["guidance"] == [
        "This operation requires human approval. Do not execute before approval."
    ]


def _evaluate_real_shell_policy(command, denied_commands=(), *, analysis_override=None):
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
    if analysis_override is not None:
        document["intent"]["shell_analysis"] = analysis_override
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
