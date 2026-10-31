# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Structured application tool-call authorization contract."""

import hashlib
import json

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


@pytest.mark.parametrize("sign", [1, -1])
def test_tool_integer_digit_boundary(sign):
    accepted = sign * (10**640 - 1)
    arguments = {"nested": [{"value": accepted}]}
    encoded = canonical_tool_arguments(arguments)
    assert json.loads(encoded) == arguments
    request = ToolCallRequest.model_validate_json(
        json.dumps(
            {"schema_version": 1, "tool_name": "example", "subject": "user", "arguments": arguments}
        )
    )
    assert request.arguments == arguments
    with pytest.raises(ValueError, match="640-decimal-digit"):
        canonical_tool_arguments({"nested": [{"value": sign * 10**640}]})


def test_integer_bound_precedes_python_decimal_conversion():
    with pytest.raises(ValidationError, match="640-decimal-digit"):
        ToolCallRequest.model_validate(_tool_call(arguments={"value": 10**4300}))


def test_raw_json_integer_over_explicit_bound_is_rejected():
    raw = (
        '{"schema_version":1,"tool_name":"example","subject":"user","arguments":{"value":'
        + "1"
        + "0" * 640
        + "}}"
    )
    with pytest.raises(ValidationError, match="640-decimal-digit"):
        ToolCallRequest.model_validate_json(raw)


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
        agent_id="app-service",
        council_id="council-prod",
        tool_call=_tool_call(),
        description="Send a requested Slack message",
    )

    assert request.api_version == "v2"
    assert request.command == ""
    assert request.working_directory == ""


async def _authorize_through_mcp(arguments):
    """Call council_authorize through NAT's MCP tool wrapper and return the validated input.

    NAT builds the MCP tool signature from the input model's fields, with their
    concrete defaults, so FastMCP passes every omitted optional field explicitly.
    """
    from contextlib import asynccontextmanager

    from mcp.server.fastmcp import FastMCP
    from nat.plugins.mcp.server.tool_converter import create_function_wrapper

    received = []

    class _Runner:
        async def result(self):
            return "ok"

    class _SessionManager:
        @asynccontextmanager
        async def run(self, payload):
            received.append(payload)
            yield _Runner()

    mcp = FastMCP(name="test-council")
    mcp.tool(name="council_authorize")(
        create_function_wrapper("council_authorize", _SessionManager(), AuthorizeInput)
    )
    await mcp.call_tool("council_authorize", arguments)
    return received[0]


@pytest.mark.asyncio
async def test_mcp_tool_call_without_api_version_is_v2():
    request = await _authorize_through_mcp(
        {"agent_id": "app-service", "council_id": "council-prod", "tool_call": _tool_call()}
    )

    assert request.api_version == "v2"


@pytest.mark.asyncio
async def test_mcp_shell_call_without_api_version_is_v1():
    request = await _authorize_through_mcp(
        {"agent_id": "agent", "command": "ls", "working_directory": "/workspace"}
    )

    assert request.api_version == "v1"


@pytest.mark.asyncio
async def test_mcp_tool_call_with_explicit_v1_is_rejected():
    with pytest.raises(Exception, match="uses api_version v2"):
        await _authorize_through_mcp(
            {
                "agent_id": "app-service",
                "council_id": "council-prod",
                "api_version": "v1",
                "tool_call": _tool_call(),
            }
        )


def test_authorize_input_rejects_mixed_shell_and_tool_call():
    with pytest.raises(ValidationError, match="cannot include shell"):
        AuthorizeInput(
            agent_id="app-service",
            council_id="council-prod",
            command="echo wrong",
            working_directory="/workspace",
            tool_call=_tool_call(),
        )


def test_intent_uses_typed_operation_without_pseudo_command():
    request = AuthorizeInput(
        agent_id="app-service",
        council_id="council-prod",
        tool_call=_tool_call(),
        description="Send a requested Slack message",
    )

    kwargs = CouncilService._build_authorize_intent_kwargs(request, "app-service")

    assert kwargs["command"] == ""
    assert kwargs["command_type"] == "api"
    assert kwargs["operation"]["tool_name"] == "slack.send_message"
    assert "arguments" not in kwargs["operation"]


def test_structured_operation_requires_tool_write_permission():
    request = ToolCallRequest.model_validate(_tool_call())
    intent = Intent.create(
        agent_id="app-service",
        description="Send a requested Slack message",
        target_resource="slack.send_message",
        command_type="api",
        operation=request.to_operation(),
    )

    assert categorize_intent(intent) == {PermissionScope.TOOL_WRITE}


def test_council_prompt_receives_exact_structured_arguments():
    request = ToolCallRequest.model_validate(_tool_call())
    intent = Intent.create(
        agent_id="app-service",
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
        agent_id="app-service",
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
        agent_id="app-service",
        description="Send a requested Slack message",
        target_resource="slack.send_message",
        command_type="api",
        operation=operation,
    )

    writ = await factory.mint_writ(
        intent=intent,
        target_agent_id="app-service",
        threshold_used="unanimous",
        member_votes=[],
        chairman_risk_level="medium",
    )

    assert isinstance(writ, ToolCallWrit)
    assert writ.verify(key_pair.public_key)
    assert writ.payload().operation == operation


@pytest.mark.parametrize(
    "subject", ["user\x00", "user\nforged", "user\x7f", "user\u2028", "user\u2029"]
)
def test_subject_rejects_prompt_and_log_control_characters(subject):
    from nomos.level_0.types.tool_call import ToolCallOperation

    with pytest.raises(ValidationError):
        ToolCallRequest.model_validate(_tool_call(subject=subject))
    operation = ToolCallRequest.model_validate(_tool_call()).to_operation().model_dump()
    operation["subject"] = subject
    with pytest.raises(ValidationError):
        ToolCallOperation.model_validate(operation)


def test_intent_rejects_command_and_structured_operation():
    with pytest.raises(ValidationError, match="cannot include a command"):
        Intent.create(
            agent_id="app-service",
            description="Mixed operation request",
            target_resource="tool",
            command="echo unrelated",
            operation=ToolCallRequest.model_validate(_tool_call()).to_operation(),
        )


@pytest.mark.parametrize(
    "extra",
    [
        {"api_version": "v1"},
        {"env": {"TOKEN": "secret"}},
        {"packaged_files": [{"path": "script.py", "content_base64": "cHJpbnQoMSk="}]},
    ],
)
def test_authorize_rejects_legacy_or_shell_fields_for_structured_calls(extra):
    with pytest.raises(ValidationError, match="structured tool-call authorization"):
        AuthorizeInput(agent_id="app-service", tool_call=_tool_call(), **extra)


def test_public_authorize_schema_describes_exclusive_shapes():
    jsonschema = pytest.importorskip("jsonschema")

    schema = AuthorizeInput.model_json_schema()
    jsonschema.validate({"agent_id": "app-service", "tool_call": _tool_call()}, schema)
    jsonschema.validate(
        {"agent_id": "app-service", "command": "ls", "working_directory": "/tmp"}, schema
    )
    for invalid in [
        {"agent_id": "app-service"},
        {
            "agent_id": "app-service",
            "tool_call": _tool_call(),
            "command": "ls",
            "working_directory": "/tmp",
        },
        {"agent_id": "app-service", "tool_call": _tool_call(), "api_version": "v1"},
    ]:
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(invalid, schema)


def test_nomos_json_v1_numeric_contract():
    assert canonical_tool_arguments(
        {"int": 1, "float": 1.0, "zero": -0.0, "tiny": 1e-7, "large": 10**30}
    ) == (b'{"float":1.0,"int":1,"large":1000000000000000000000000000000,"tiny":1e-07,"zero":-0.0}')


def test_structured_request_context_is_preserved_in_intent_and_review():
    context = "USER REQUEST: send hello to C123; prior result: recipient confirmed"
    request = AuthorizeInput(agent_id="app-service", tool_call=_tool_call(), context=context)
    intent = Intent.create(**CouncilService._build_authorize_intent_kwargs(request, "app-service"))
    intent._tool_review_material = request.tool_call
    assert intent.context == context
    assert intent.model_dump()["context"] == context
    assert context in _format_intent_for_review(intent)


@pytest.mark.parametrize("version", [1, "2", 2.0, True, None, 3])
def test_deserialize_does_not_downgrade_versioned_shell_envelopes(version):
    import base64
    import json

    legacy = {
        "writ_id": "writ-test",
        "intent_hash": "0" * 64,
        "command": "ls",
        "target_agent_id": "app-service",
        "issued_at": 1.0,
        "expires_at": 2.0,
        "nonce": "nonce",
        "writ_version": version,
    }
    blob = base64.b64encode(json.dumps(legacy).encode()).decode()
    with pytest.raises(ValidationError):
        WritFactory.deserialize_writ(blob)


def test_deserialize_rejects_invalid_outer_base64():
    import base64
    import binascii
    import json

    legacy = {
        "writ_id": "writ-test",
        "intent_hash": "0" * 64,
        "command": "ls",
        "target_agent_id": "app-service",
        "issued_at": 1.0,
        "expires_at": 2.0,
        "nonce": "nonce",
    }
    blob = base64.b64encode(json.dumps(legacy).encode()).decode()
    with pytest.raises(binascii.Error):
        WritFactory.deserialize_writ("!" + blob)


@pytest.mark.asyncio
async def test_structured_missing_permission_denies_before_council(monkeypatch):
    from nomos.auth.authenticator import AgentIdentity
    from nomos.config.settings import CouncilSettings, OPAConfig

    service = CouncilService(
        CouncilSettings(
            council_id="test",
            require_auth=False,
            members=[],
            opa=OPAConfig(enabled=False),
            council_key_path="/nonexistent/nomos-tool-tests/keys",
        )
    )
    identity = AgentIdentity(
        agent_id="app-service", display_name="App", permissions={"shell:execute"}, councils={"test"}
    )

    async def resolve(*args):
        return identity, ""

    monkeypatch.setattr(service, "_resolve_request_agent", resolve)
    result = await service.authorize(
        AuthorizeInput(agent_id="app-service", council_id="test", tool_call=_tool_call())
    )
    assert result.status == "denied"
    assert result.missing_permissions == ["tool:write"]
    assert result.writ is None
    assert service.council.get_active_workflows() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approve", "deny"])
async def test_structured_service_review_signature_digest_and_denial_audit(
    tmp_path, monkeypatch, decision
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from nomos.config.settings import CouncilSettings, OPAConfig
    from nomos.level_0.types import CouncilMember
    from nomos.llm.council_agent import IntentEvaluation

    service = CouncilService(
        CouncilSettings(
            council_id="test",
            require_auth=False,
            members=[],
            opa=OPAConfig(enabled=False),
            council_key_path=str(tmp_path / "keys"),
        )
    )
    received = []

    async def evaluate(intent):
        received.append(intent)
        return IntentEvaluation(decision=decision, reasoning="Member review result")

    service._agents.append(
        SimpleNamespace(
            member_id="member",
            display_name="Member",
            provider_name="fake",
            evaluate_intent=evaluate,
        )
    )
    service.council.register_council_member(
        CouncilMember(member_id="member", public_key_hex="", display_name="Member", weight=1)
    )
    durable = SimpleNamespace(store_intent=AsyncMock())
    if decision == "deny":
        monkeypatch.setattr(service, "_durable", lambda: durable)
    request = AuthorizeInput(
        agent_id="app-service",
        council_id="test",
        tool_call=_tool_call(),
        justification="The user asked to send this message",
        context="USER REQUEST: send hello",
    )
    result = await service.authorize(request)
    assert len(received) == 1
    assert received[0].context == request.context
    assert received[0]._tool_review_material.arguments == request.tool_call.arguments
    if decision == "approve":
        assert result.status == "approved", result.error
        writ = WritFactory.deserialize_writ(result.writ)
        assert isinstance(writ, ToolCallWrit)
        assert writ.verify(service._active_signer().public_key_bytes())
        assert writ.payload().operation == request.tool_call.to_operation()
    else:
        assert result.status == "denied"
        assert result.writ is None
        audit = durable.store_intent.call_args.args[1]
        assert audit["operation"] == request.tool_call.to_operation().model_dump(mode="json")
        assert "arguments" not in audit["operation"]


@pytest.mark.asyncio
async def test_v2_verifies_original_payload_bytes_without_reserialization():
    import base64
    import json

    key_pair = KeyPair.generate(key_id="council-test")
    factory = WritFactory(council_key_pair=key_pair, council_id="test")
    intent = Intent.create(
        agent_id="app-service",
        description="Authorize a structured call",
        target_resource="tool",
        operation=ToolCallRequest.model_validate(_tool_call()).to_operation(),
    )
    writ = await factory.mint_writ(
        intent=intent,
        target_agent_id="app-service",
        threshold_used="unanimous",
        member_votes=[],
        chairman_risk_level="medium",
    )
    # A legitimate signer may use whitespace and a different JSON key order.
    original = json.dumps(writ.payload().model_dump(mode="json"), indent=2).encode()
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    signature = Ed25519PrivateKey.from_private_bytes(key_pair.private_key).sign(original)
    envelope = ToolCallWrit(
        payload_base64=base64.b64encode(original).decode(), signature=signature.hex()
    )
    restored = WritFactory.deserialize_writ(WritFactory.serialize_writ(envelope))
    assert restored.signed_bytes() == original
    assert restored.verify(key_pair.public_key)
    reserialized = restored.model_copy(
        update={"payload_base64": base64.b64encode(restored.payload().canonical_bytes()).decode()}
    )
    assert not reserialized.verify(key_pair.public_key)
