# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Structured application tool-call authorization contract."""

import hashlib

import pytest
from pydantic import ValidationError

from nomos.auth.permissions import PermissionScope, categorize_intent
from nomos.auth.writ_factory import WritFactory
from nomos.level_0.crypto.signing import KeyPair
from nomos.level_0.types import Intent, ToolCallWrit
from nomos.level_0.types.tool_call import ToolCallRequest, canonical_tool_arguments
from nomos.llm.council_agent import _format_intent_for_review
from nomos.nat.functions import AuthorizeInput, CouncilService
from nomos.opa.client import build_intent_filter_input


def _tool_call(**overrides):
    value = {
        "schema_version": 1,
        "tool_name": "slack.send_message",
        "arguments": {"channel": "C123", "message": "hello"},
        "subject": "opaque-user-7d9a",
    }
    value.update(overrides)
    return value


def test_tool_call_request_derives_operation_digest_server_side():
    request = ToolCallRequest.model_validate(_tool_call())

    operation = request.to_operation()

    expected = canonical_tool_arguments({"channel": "C123", "message": "hello"})
    assert operation.kind == "tool_call"
    assert operation.tool_name == "slack.send_message"
    assert operation.arguments_sha256 == hashlib.sha256(expected).hexdigest()
    assert operation.subject == "opaque-user-7d9a"


def test_tool_call_request_rejects_a_caller_supplied_digest():
    with pytest.raises(ValidationError, match="arguments_sha256"):
        ToolCallRequest.model_validate(_tool_call(arguments_sha256="0" * 64))


def test_tool_call_request_snapshots_mutable_arguments():
    arguments = {"channel": "C123", "nested": {"message": "original"}}
    request = ToolCallRequest.model_validate(_tool_call(arguments=arguments))

    arguments["nested"]["message"] = "changed"

    assert request.arguments["nested"]["message"] == "original"


def test_canonical_tool_arguments_is_stable_for_nested_key_order_and_unicode():
    left = {"outer": {"z": 1, "a": "café"}, "items": [1, 1.0, True, None]}
    right = {"items": [1, 1.0, True, None], "outer": {"a": "café", "z": 1}}

    assert canonical_tool_arguments(left) == canonical_tool_arguments(right)
    assert b"caf\xc3\xa9" in canonical_tool_arguments(left)


def test_nomos_json_v1_cross_repository_vector():
    arguments = {"metadata": {"z": [True, None, 1.0], "a": 1}, "channel": "café"}

    encoded = canonical_tool_arguments(arguments)
    expected_digest = "1bcab5a883853a1708ba45d593afd3a70676d538e09419967e36da76b19a0e1d"  # pragma: allowlist secret  # noqa: E501

    assert encoded == b'{"channel":"caf\xc3\xa9","metadata":{"a":1,"z":[true,null,1.0]}}'
    assert hashlib.sha256(encoded).hexdigest() == expected_digest


@pytest.mark.parametrize("arguments", [{"value": float("nan")}, {"value": float("inf")}])
def test_tool_call_request_rejects_non_finite_arguments(arguments):
    with pytest.raises(ValidationError):
        ToolCallRequest.model_validate(_tool_call(arguments=arguments))


def test_authorize_input_infers_v2_from_structured_tool_call():
    request = AuthorizeInput(
        agent_id="pa2-service",
        council_id="council-prod",
        tool_call=_tool_call(),
        description="Send a requested Slack message",
    )

    assert request.api_version == "v2"
    assert request.command == ""
    assert request.working_directory == ""


def test_authorize_input_rejects_mixed_shell_and_tool_call():
    with pytest.raises(ValidationError, match="cannot include shell"):
        AuthorizeInput(
            agent_id="pa2-service",
            council_id="council-prod",
            command="echo wrong",
            working_directory="/workspace",
            tool_call=_tool_call(),
        )


def test_intent_uses_typed_operation_without_pseudo_command():
    request = AuthorizeInput(
        agent_id="pa2-service",
        council_id="council-prod",
        tool_call=_tool_call(),
        description="Send a requested Slack message",
    )

    kwargs = CouncilService._build_authorize_intent_kwargs(request, "pa2-service")

    assert kwargs["command"] == ""
    assert kwargs["command_type"] == "api"
    assert kwargs["operation"]["tool_name"] == "slack.send_message"
    assert "arguments" not in kwargs["operation"]


def test_structured_operation_requires_tool_write_permission():
    request = ToolCallRequest.model_validate(_tool_call())
    intent = Intent.create(
        agent_id="pa2-service",
        description="Send a requested Slack message",
        target_resource="slack.send_message",
        command_type="api",
        operation=request.to_operation(),
    )

    assert categorize_intent(intent) == {PermissionScope.TOOL_WRITE}


def test_council_prompt_receives_exact_structured_arguments():
    request = ToolCallRequest.model_validate(_tool_call())
    intent = Intent.create(
        agent_id="pa2-service",
        description="Send a requested Slack message",
        target_resource="slack.send_message",
        command_type="api",
        operation=request.to_operation(),
    )
    intent._tool_review_material = request

    prompt = _format_intent_for_review(intent)

    assert "Proposed Tool Operation" in prompt
    assert '"tool_name":"slack.send_message"' in prompt
    assert '"arguments":{"channel":"C123","message":"hello"}' in prompt


def test_durable_and_opa_views_exclude_raw_tool_arguments():
    request = ToolCallRequest.model_validate(_tool_call())
    intent = Intent.create(
        agent_id="pa2-service",
        description="Send a requested Slack message",
        target_resource="slack.send_message",
        command_type="api",
        operation=request.to_operation(),
    )
    intent._tool_review_material = request

    durable = intent.model_dump(mode="json")
    opa_input = build_intent_filter_input(
        intent,
        request_type="authorize",
        authenticated=True,
        agent_permissions={"tool:write"},
        required_permissions={"tool:write"},
    )

    assert "_tool_review_material" not in durable
    assert "arguments" not in durable["operation"]
    assert "arguments" not in opa_input["intent"]["operation"]
    assert opa_input["intent"]["operation"]["arguments_sha256"]


@pytest.mark.asyncio
async def test_writ_factory_mints_signed_tool_call_writ():
    key_pair = KeyPair.generate(key_id="council-test")
    factory = WritFactory(council_key_pair=key_pair, council_id="council-test")
    operation = ToolCallRequest.model_validate(_tool_call()).to_operation()
    intent = Intent.create(
        agent_id="pa2-service",
        description="Send a requested Slack message",
        target_resource="slack.send_message",
        command_type="api",
        operation=operation,
    )

    writ = await factory.mint_writ(
        intent=intent,
        target_agent_id="pa2-service",
        threshold_used="unanimous",
        member_votes=[],
        chairman_risk_level="medium",
    )

    assert isinstance(writ, ToolCallWrit)
    assert writ.verify(key_pair.public_key)
    assert writ.payload().operation == operation
