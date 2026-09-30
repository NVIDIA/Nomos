# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Structured PA tool-call authorization and signed-writ tests."""

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from nomos.auth.permissions import PermissionScope, categorize_intent
from nomos.auth.writ_factory import TOOL_CALL_OPERATION_CONTEXT_KEY, WritFactory
from nomos.level_0.crypto.signing import KeyPair
from nomos.level_0.types import Intent, ToolCallAuthorization, ToolCallOperation, ToolCallWrit
from nomos.nat.functions import AuthorizeInput, CouncilService


def test_shared_cross_language_signing_vector():
    """PA2 consumes this identical fixture; floats retain Python's signed bytes."""
    vector = json.loads((Path(__file__).parent / "fixtures/tool_call_writ_v2.json").read_text())
    writ = WritFactory.deserialize_writ(vector["writ_blob"])
    assert isinstance(writ, ToolCallWrit)
    assert writ.verify(bytes.fromhex(vector["public_key_hex"]))
    assert writ.payload().model_dump(mode="json") == vector["payload"]
    assert WritFactory.serialize_writ(writ) == vector["writ_blob"]


def test_arguments_limit_fits_live_intent_context():
    args = '{"text":"' + "a" * 31_989 + '"}'
    assert len(args) == 32_000
    authorization = _authorization(args)
    kwargs = CouncilService._build_authorize_intent_kwargs(
        AuthorizeInput(agent_id="pa2-service", tool_call=authorization), "pa2-service"
    )
    assert Intent.create(**kwargs).context == args
    with pytest.raises(ValidationError):
        _authorization(args.replace("a", "aa", 1))


def _authorization(arguments_json: str = '{"channel":"C123","message":"hello"}'):
    return ToolCallAuthorization(
        operation=ToolCallOperation(
            tool_name="slack--send_message",
            arguments_sha256=hashlib.sha256(arguments_json.encode()).hexdigest(),
            subject="pa2:user-7",
        ),
        arguments_json=arguments_json,
    )


def _intent(operation: ToolCallOperation) -> Intent:
    return Intent.create(
        agent_id="pa2-service",
        action_type="network.modify",
        description="Send an approved Slack message",
        target_resource="slack--send_message",
        command=(
            f"PA tool call {operation.tool_name} arguments_sha256={operation.arguments_sha256}"
        ),
        command_type="api",
        execution_context={TOOL_CALL_OPERATION_CONTEXT_KEY: operation.model_dump(mode="json")},
    )


def test_tool_call_authorization_rejects_digest_mismatch():
    with pytest.raises(ValidationError, match="digest does not match"):
        ToolCallAuthorization(
            operation=ToolCallOperation(
                tool_name="jira--jira_create_issue",
                arguments_sha256="0" * 64,
                subject="pa2:user-7",
            ),
            arguments_json='{"project":"PA"}',
        )


def test_tool_calls_require_dedicated_permission_without_shell_rights():
    assert categorize_intent(_intent(_authorization().operation)) == {PermissionScope.TOOL_WRITE}


def test_tool_call_authorization_rejects_duplicate_json_keys():
    arguments_json = '{"message":"first","message":"second"}'
    with pytest.raises(ValidationError, match="duplicate object keys"):
        _authorization(arguments_json)


def test_authorize_input_requires_exactly_one_operation_shape():
    tool_call = _authorization()
    with pytest.raises(ValidationError, match="cannot include shell"):
        AuthorizeInput(
            agent_id="pa2-service",
            council_id="council-prod",
            command="/bin/true",
            working_directory="/tmp",
            tool_call=tool_call,
        )
    with pytest.raises(ValidationError, match="requires command"):
        AuthorizeInput(agent_id="pa2-service", council_id="council-prod")


def test_tool_call_intent_uses_live_arguments_only_as_context():
    tool_call = _authorization()
    kwargs = CouncilService._build_authorize_intent_kwargs(
        AuthorizeInput(
            agent_id="pa2-service",
            council_id="council-prod",
            tool_call=tool_call,
        ),
        "pa2-service",
    )

    assert kwargs["context"] == tool_call.arguments_json
    assert kwargs["command_type"] == "api"
    assert json.loads(json.dumps(kwargs["execution_context"])) == {
        TOOL_CALL_OPERATION_CONTEXT_KEY: tool_call.operation.model_dump(mode="json")
    }
    assert "hello" not in kwargs["command"]


@pytest.mark.asyncio
async def test_tool_call_writ_signs_immutable_payload_bytes_and_round_trips():
    key_pair = KeyPair.generate(key_id="council-test")
    factory = WritFactory(council_key_pair=key_pair, council_id="council-prod")
    operation = _authorization().operation

    writ = await factory.mint_writ(
        intent=_intent(operation),
        target_agent_id="pa2-service",
        threshold_used="unanimous",
        member_votes=[],
        chairman_risk_level="medium",
    )

    assert isinstance(writ, ToolCallWrit)
    assert writ.verify(key_pair.public_key)
    assert writ.payload().operation == operation
    assert "hello" not in writ.signed_bytes().decode()

    restored = WritFactory.deserialize_writ(WritFactory.serialize_writ(writ))
    assert isinstance(restored, ToolCallWrit)
    assert restored.signed_bytes() == writ.signed_bytes()
    assert restored.signature == writ.signature
    assert restored.verify(key_pair.public_key)


@pytest.mark.asyncio
async def test_tool_call_writ_rejects_signed_payload_tampering():
    key_pair = KeyPair.generate(key_id="council-test")
    factory = WritFactory(council_key_pair=key_pair, council_id="council-prod")
    writ = await factory.mint_writ(
        intent=_intent(_authorization().operation),
        target_agent_id="pa2-service",
        threshold_used="unanimous",
        member_votes=[],
        chairman_risk_level="medium",
    )
    assert isinstance(writ, ToolCallWrit)

    tampered = writ.model_copy(update={"payload_base64": writ.payload_base64[:-4] + "AAAA"})
    assert not tampered.verify(key_pair.public_key)
