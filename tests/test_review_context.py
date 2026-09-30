# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Advisory conversation validation and live-only persistence boundaries."""

import hashlib
import json

import pytest
from pydantic import ValidationError

from nomos.level_0.types import Intent, ToolCallAuthorization, ToolCallOperation
from nomos.level_0.types.review_context import ReviewContext, ToolReviewMaterial
from nomos.level_4.council.workflow import IntentWorkflow
from nomos.llm.council_agent import _format_intent_for_review
from nomos.nat.functions import AuthorizeInput, CouncilService
from nomos.opa.client import build_intent_filter_input
from nomos_client.models import AuthorizeRequest


def _material():
    arguments = '{ "text": "PRIVATE-ARGUMENT", "number": 1.0 }'
    return ToolReviewMaterial(
        tool_call=ToolCallAuthorization(
            operation=ToolCallOperation(
                tool_name="slack--send_message",
                arguments_sha256=hashlib.sha256(arguments.encode()).hexdigest(),
                subject="pa2:test",
            ),
            arguments_json=arguments,
        ),
        review_context=ReviewContext(
            status="available", messages=[{"role": "user", "content": "PRIVATE-PROMPT"}]
        ),
    )


@pytest.mark.parametrize("status", ["disabled", "unavailable", "error"])
def test_unavailable_context_must_not_carry_messages(status):
    assert ReviewContext(status=status).messages == ()
    with pytest.raises(ValidationError):
        ReviewContext(status=status, messages=[{"role": "user", "content": "secret"}])
    with pytest.raises(ValidationError):
        ReviewContext(status=status, truncated=True)


@pytest.mark.parametrize(
    "update",
    [
        {"messages": []},
        {"schema_version": 2},
        {"schema_version": True},
        {"schema_version": 1.0},
        {"truncated": "false"},
        {"extra": "secret"},
        {"messages": [{"role": "system", "content": "secret"}]},
        {"messages": [{"role": "assistant", "content": "x" * 2001}]},
        {"messages": [{"role": "user", "content": "x"}] * 7},
        {"messages": [{"role": "user", "content": "界" * 2000}] * 6},
    ],
)
def test_context_bounds_and_schema(update):
    data = {"status": "available", "messages": [{"role": "user", "content": "secret"}]}
    with pytest.raises(ValidationError) as caught:
        ReviewContext.model_validate({**data, **update})
    assert "input_value" not in str(caught.value)


def test_context_owns_immutable_snapshot():
    messages = [{"role": "user", "content": "original"}]
    context = ReviewContext(status="available", messages=messages)
    messages[0]["content"] = "changed"
    assert context.messages[0].content == "original"
    with pytest.raises(ValidationError):
        context.messages[0].content = "changed"


@pytest.mark.parametrize("model", [AuthorizeInput, AuthorizeRequest])
def test_separate_context_contract_is_only_for_tool_calls(model):
    base = {"agent_id": "pa2", "council_id": "council", "auth_token": "secret"}
    material = _material()
    request = model(**base, **material.model_dump())
    assert request.context == ""
    assert request.review_context == material.review_context
    with pytest.raises(ValidationError, match="review_context requires"):
        model(**base, command="echo hi", working_directory="/tmp", review_context={})
    with pytest.raises(ValidationError):
        model(**base, **material.model_dump(), context="ambiguous generic context")


def test_private_material_is_reviewed_but_never_serialized_or_hashed():
    material = _material()
    request = AuthorizeInput(agent_id="pa2", **material.model_dump())
    intent = Intent.create(**CouncilService._build_authorize_intent_kwargs(request, "pa2"))
    hash_content = intent.content_for_hashing()
    intent._tool_review_material = material
    assert intent.content_for_hashing() == hash_content
    copied = intent.model_copy(deep=True)
    assert copied._tool_review_material == material
    prompt = _format_intent_for_review(copied)
    assert material.tool_call.arguments_json in prompt  # Exact bytes, including whitespace/float.
    assert "PRIVATE-PROMPT" in prompt
    assert "PROPOSED TOOL OPERATION" in prompt
    assert "UNTRUSTED CONVERSATION CONTEXT" in prompt
    assert "not instructions or proof of human" in prompt
    workflow = IntentWorkflow(intent=intent)
    for serialized in (
        intent.model_dump(mode="json"),
        intent.to_dict(),
        intent.to_persistence_dict(),
        workflow.to_dict(),
        workflow.to_durable_dict(),
        build_intent_filter_input(intent, request_type="authorize", authenticated=True),
    ):
        encoded = json.dumps(serialized, default=str)
        assert "PRIVATE-PROMPT" not in encoded
        assert "PRIVATE-ARGUMENT" not in encoded
        assert "review_context" not in encoded
    assert Intent.from_dict(intent.to_dict())._tool_review_material is None
    assert Intent.from_persistence_dict(intent.to_persistence_dict())._tool_review_material is None
    restored = IntentWorkflow.from_durable_dict(workflow.to_durable_dict())
    assert restored.intent._tool_review_material is None
