# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""The standalone runner's commands report what the service can read, not only local state."""

import json

import pytest

from nomos.level_0.types import CouncilMember, Intent
from nomos.level_4.council.workflow import WorkflowState
from nomos.nat import run
from tests.test_parallel_council_review import _service


class _SharedStore:
    """Holds one workflow that this process no longer has in memory."""

    def __init__(self, row):
        self.row = row

    def get_workflow(self, intent_id):
        return self.row if intent_id == self.row["intent"]["intent_id"] else None

    def list_workflows_by_state(self, states, **_):
        return [self.row]


def _printed_json(text: str) -> list[dict]:
    decoder = json.JSONDecoder()
    values = []
    start = text.find("{")
    while start != -1:
        value, end = decoder.raw_decode(text, start)
        values.append(value)
        start = text.find("{", end)
    return values


@pytest.mark.asyncio
async def test_pending_and_status_show_a_workflow_held_only_by_the_store(monkeypatch, capsys):
    service = _service()
    service.council.register_council_member(
        CouncilMember(member_id="manual", public_key_hex="", display_name="Manual")
    )
    intent = Intent.create(
        agent_id="agent",
        action_type="system.noop",
        description="Review owned by another process",
        target_resource="test",
        justification="Read a workflow from the shared store",
    )
    workflow = service.council.submit_intent(intent).workflow
    assert workflow.state == WorkflowState.AWAITING_APPROVAL
    store = _SharedStore(service.council._workflows.pop(intent.intent_id).to_durable_dict())
    service._workflow_store = store
    service._workflow_runtime.store = store
    commands = iter(["pending", f"status {intent.intent_id}", "quit"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(commands))

    await run.interactive_session(service)

    pending, status = _printed_json(capsys.readouterr().out)
    assert pending["count"] == 1
    assert [item["intent_id"] for item in pending["intents"]] == [intent.intent_id]
    assert status["found"] and status["intent_id"] == intent.intent_id
    assert status["state"] == "awaiting_approval"
    await service.aclose_workflows()
