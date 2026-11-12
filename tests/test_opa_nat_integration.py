# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import pytest

from nomos.config.settings import CouncilSettings, OPAConfig
from nomos.level_4.council.workflow import WorkflowState
from nomos.nat.functions import AuthorizeInput, CouncilService, ProposeIntentInput
from nomos.opa import OPAEvaluationError, OPAPolicyDecision


class RecordingOPAClient:
    def __init__(self, decision: OPAPolicyDecision | None = None, error: Exception | None = None):
        self.decision = decision or OPAPolicyDecision(allow=True)
        self.error = error
        self.calls = []

    async def evaluate(self, input_doc):
        self.calls.append(input_doc)
        if self.error is not None:
            raise self.error
        return self.decision


def _make_service(tmp_path, *, fail_closed: bool = True) -> CouncilService:
    settings = CouncilSettings(
        council_id="opa-test-council",
        require_auth=False,
        require_justification=True,
        llm_backends={},
        members=[],
        council_key_path=str(tmp_path),
        opa=OPAConfig(enabled=True, fail_closed=fail_closed),
    )
    return CouncilService(settings)


@pytest.mark.asyncio
async def test_opa_deny_blocks_propose_before_council_workflow(tmp_path):
    service = _make_service(tmp_path)
    opa = RecordingOPAClient(
        OPAPolicyDecision(
            allow=False,
            reasons=["target resource is outside allowed deployment scope"],
            guidance=["Use a /workspace target."],
        )
    )
    service._opa_client = opa

    result = await service.propose_intent(
        ProposeIntentInput(
            agent_id="agent-1",
            description="Run a harmless absolute command",
            target_resource="/tmp",
            command="/bin/echo hello",
            working_directory="/tmp",
            justification="Testing OPA deny path",
        )
    )

    assert result.success is True
    assert result.state == WorkflowState.DENIED.value
    assert "Denied by OPA policy" in result.error
    assert "outside allowed deployment scope" in result.error
    assert service.council.get_workflow(result.intent_id) is None
    assert opa.calls[0]["request"]["type"] == "propose_intent"
    assert opa.calls[0]["council"]["id"] == "opa-test-council"
    assert opa.calls[0]["council"]["opa_policy_id"] == "strict-shell"
    assert opa.calls[0]["intent"]["command"] == "/bin/echo hello"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "expected_permissions"),
    [
        ("/bin/ls /tmp", {"shell:read"}),
        ("/bin/cat /etc/hosts", {"shell:read", "file:read"}),
        ("/bin/echo data > /tmp/out.txt", {"file:write"}),
        ("/bin/rm /tmp/somefile", {"shell:write", "file:delete"}),
        ("/bin/mkdir /tmp/newdir", {"shell:write"}),
        ("/usr/local/bin/kubectl apply -f deploy.yaml", {"infra:deploy"}),
        ("/usr/local/bin/kubectl delete pod my-pod", {"infra:destroy"}),
    ],
)
async def test_propose_opa_receives_permissions_from_top_level_command(
    tmp_path, command, expected_permissions
):
    service = _make_service(tmp_path)
    opa = RecordingOPAClient(
        OPAPolicyDecision(
            allow=False,
            reasons=["stop after OPA capture"],
        )
    )
    service._opa_client = opa

    result = await service.propose_intent(
        ProposeIntentInput(
            agent_id="agent-1",
            description="Capture required permissions from command",
            target_resource="/tmp",
            command=command,
            working_directory="/tmp",
            justification="Testing OPA required permission input",
        )
    )

    assert result.state == WorkflowState.DENIED.value
    assert set(opa.calls[0]["required_permissions"]) == expected_permissions


@pytest.mark.asyncio
async def test_opa_deny_blocks_authorize_before_writ_minting(tmp_path):
    service = _make_service(tmp_path)
    opa = RecordingOPAClient(
        OPAPolicyDecision(
            allow=False,
            reasons=["production changes require a maintenance window"],
            guidance=["Add maintenance-window evidence."],
        )
    )
    service._opa_client = opa

    result = await service.authorize(
        AuthorizeInput(
            agent_id="agent-1",
            command="/bin/echo deploy",
            working_directory="/tmp",
            description="Deploy a production configuration change",
            target_resource="prod/service",
            justification="Testing OPA authorize deny path",
            blast_radius="One service",
        )
    )

    assert result.status == "denied"
    assert result.writ is None
    assert result.decision_id.startswith("intent-")
    assert "production changes require" in result.error
    assert result.retryable is False
    assert "retryable" not in result.model_dump(mode="json")
    assert "require fresh authorization" in result.guidance[0]
    assert result.guidance[1:] == ["Add maintenance-window evidence."]
    assert result.model_dump(mode="json")["guidance"] == result.guidance
    assert service.council.get_workflow(result.decision_id) is None
    assert opa.calls[0]["request"]["type"] == "authorize"


@pytest.mark.asyncio
async def test_opa_receives_shell_command_words_for_non_absolute_segments(tmp_path):
    service = _make_service(tmp_path)
    opa = RecordingOPAClient(
        OPAPolicyDecision(
            allow=False,
            reasons=[
                "Shell commands must invoke executables by absolute path; "
                "non-absolute command word(s): python3"
            ],
            guidance=["Use absolute executable paths for every shell command segment."],
        )
    )
    service._opa_client = opa

    result = await service.authorize(
        AuthorizeInput(
            agent_id="agent-1",
            command="/bin/echo hello | python3 scripts/run.py",
            working_directory="/tmp",
            description="Run a pipeline with one relative executable",
            target_resource="/tmp",
            justification="Testing OPA shell command word input",
        )
    )

    assert result.status == "denied"
    assert "non-absolute command word(s): python3" in result.error
    assert opa.calls[0]["intent"]["shell_command_words"] == ["/bin/echo", "python3"]


@pytest.mark.asyncio
async def test_opa_receives_per_council_profile_settings(tmp_path):
    settings = CouncilSettings(
        council_id="git-council",
        require_auth=False,
        require_justification=True,
        llm_backends={},
        members=[],
        council_key_path=str(tmp_path),
        opa=OPAConfig(
            enabled=True,
            policy_id="git-workflow-local",
            relaxed_working_dir_prefixes=["/workspace/projects"],
        ),
    )
    service = CouncilService(settings)
    opa = RecordingOPAClient(
        OPAPolicyDecision(
            allow=False,
            reasons=["stop after OPA capture"],
        )
    )
    service._opa_client = opa

    result = await service.propose_intent(
        ProposeIntentInput(
            agent_id="agent-1",
            council_id="git-council",
            description="Inspect the local repository status",
            target_resource="git://example/repo",
            command="git status --porcelain",
            working_directory="/workspace/projects/example",
            justification="Testing per-council OPA profile input",
        )
    )

    assert result.state == WorkflowState.DENIED.value
    assert opa.calls[0]["council"] == {
        "id": "git-council",
        "opa_policy_id": "git-workflow-local",
        "opa_relaxed_bare_command_words": [],
        "opa_relaxed_working_dir_prefixes": ["/workspace/projects"],
    }


@pytest.mark.asyncio
async def test_opa_fail_closed_denies_when_opa_is_unavailable(tmp_path):
    service = _make_service(tmp_path, fail_closed=True)
    service._opa_client = RecordingOPAClient(
        error=OPAEvaluationError("connection refused"),
    )

    result = await service.authorize(
        AuthorizeInput(
            agent_id="agent-1",
            command="/bin/echo deploy",
            working_directory="/tmp",
            description="Run a harmless command",
            target_resource="/tmp",
            justification="Testing OPA fail closed",
        )
    )

    assert result.status == "denied"
    assert "OPA evaluation failed" in result.error
    assert result.writ is None


@pytest.mark.asyncio
async def test_opa_fail_open_allows_existing_flow_to_continue(tmp_path):
    service = _make_service(tmp_path, fail_closed=False)
    service._opa_client = RecordingOPAClient(
        error=OPAEvaluationError("connection refused"),
    )

    result = await service.propose_intent(
        ProposeIntentInput(
            agent_id="agent-1",
            description="Run a harmless absolute command",
            target_resource="/tmp",
            command="/bin/echo hello",
            working_directory="/tmp",
            justification="Testing OPA fail open",
        )
    )

    assert result.success is True
    assert result.intent_id.startswith("intent-")
    assert service.council.get_workflow(result.intent_id) is not None
