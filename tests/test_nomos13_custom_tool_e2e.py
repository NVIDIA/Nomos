# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""One bespoke tool, end to end through ``CouncilService.authorize``.

The council is loaded from YAML with ``load_config``; authentication, the
permission and membership gates, the catalog resolver, the schema worker, the
council members, vote counting and writ minting are the real code. Only the
external boundaries are replaced: the LLM backend (a recorder that approves),
OPA's HTTP evaluation (records its input and allows), token lookup, and the
durable store (an in-memory trusted-executor directory).

The last test pins what the council does today when a NeMo Relay plugin's
certified ``nomos.observed_tool_definitions`` item disagrees with the
operator's catalog. It records current behavior; it does not endorse it.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

from nomos.auth.authenticator import AgentAuthenticator, AgentIdentity
from nomos.auth.writ_factory import WritFactory
from nomos.config.settings import load_config
from nomos.llm.base import LLMResponse, LLMRole
from nomos.nat.functions import AuthorizeInput, CouncilService
from nomos.opa.client import OPAHttpClient, OPAPolicyDecision

COUNCIL = "key-ops"
AGENT = "ops-agent"
TOOL = "secrets.rotate_api_key"

COUNCIL_YAML = f"""\
council_id: {COUNCIL}
require_auth: true
require_council_membership: true
require_justification: true
llm_max_retries: 0
llm_backends:
  review:
    provider: openai
    model: e2e-fixture-model
members:
  - id: blast-radius
    backend: review
    priorities:
      - Check the rotation is limited to the service and environment the user named.
  - id: intent-match
    backend: review
    priorities:
      - Check the call matches the user's request.
chairman:
  enabled: false
opa:
  enabled: true
  fail_closed: true
tools:
  - name: {TOOL}
    description: >-
      Issue a new API key for one service in one environment and revoke the old
      key after the grace period. Callers holding the old key fail after the
      grace period ends.
    parameters:
      type: object
      properties:
        service:
          type: string
          enum: [billing-api, search-api]
        environment:
          type: string
          enum: [staging, production]
        grace_period_minutes:
          type: integer
          minimum: 5
          maximum: 60
      required: [service, environment, grace_period_minutes]
      additionalProperties: false
    usage_constraints:
      - Rotate production keys only when the user named the service and production.
      - One service per call; never rotate keys the user did not ask about.
"""

GOOD_ARGS = {"service": "billing-api", "environment": "production", "grace_period_minutes": 15}

# What the operator says the tool is, as reviewers and OPA should see it.
CATALOG_DEFINITION = {
    "name": TOOL,
    "description": (
        "Issue a new API key for one service in one environment and revoke the old "
        "key after the grace period. Callers holding the old key fail after the "
        "grace period ends."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "service": {"type": "string", "enum": ["billing-api", "search-api"]},
            "environment": {"type": "string", "enum": ["staging", "production"]},
            "grace_period_minutes": {"type": "integer", "minimum": 5, "maximum": 60},
        },
        "required": ["service", "environment", "grace_period_minutes"],
        "additionalProperties": False,
    },
    "usage_constraints": [
        "Rotate production keys only when the user named the service and production.",
        "One service per call; never rotate keys the user did not ask about.",
    ],
}

# What the application offered the model under the same name: a different,
# broader contract. Relay certifies that this is what the model saw.
RELAY_OFFERED_DESCRIPTION = "RELAY_OFFERED: rotate every key for any service immediately"
RELAY_OFFERED = {
    "type": "function",
    "function": {
        "name": TOOL,
        "description": RELAY_OFFERED_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {"service": {"type": "string"}, "revoke_now": {"type": "boolean"}},
        },
    },
}

# plugin.py TOOL_DEFINITIONS_COMMAND; plugin.py itself needs the Relay runtime.
RELAY_TOOL_DEFINITIONS_COMMAND = (
    "(no command ran) nomos-relay-plugin observed the tool definitions the application "
    "offered the model in the request this call answers; stdout is that observation"
)
RELAY_PACKAGE = (
    Path(__file__).resolve().parents[1] / "integrations" / "nemo-relay" / "nemo_relay_nomos"
)


class RecordingBackend:
    """The LLM boundary: records every message list and approves."""

    def __init__(self):
        self.calls = []

    def provider_name(self):
        return "e2e-recorder"

    async def complete(self, messages, **kwargs):
        self.calls.append(copy.deepcopy(messages))
        return LLMResponse(
            content=json.dumps(
                {
                    "decision": "approve",
                    "reasoning": "Fixture approval.",
                    "risk_concerns": [],
                    "conditions": [],
                    "guidance": "",
                }
            )
        )


class DurableStub:
    """In-memory durable store: a trusted-executor directory plus writ/intent sinks."""

    def __init__(self, trusted=None):
        self.trusted = trusted or {}
        self.writs = {}
        self.intents = {}

    async def get_trusted_executor(self, key_fingerprint):
        return self.trusted.get(key_fingerprint)

    async def store_writ(self, writ_id, data, *args, **kwargs):
        self.writs[writ_id] = data

    async def store_intent(self, intent_id, data, *args, **kwargs):
        self.intents[intent_id] = data


@pytest.fixture
def council(monkeypatch, tmp_path):
    backend = RecordingBackend()
    opa_inputs = []
    mints = []
    original_mint = WritFactory.mint_writ

    async def evaluate(self, document):
        opa_inputs.append(copy.deepcopy(document))
        return OPAPolicyDecision(allow=True)

    async def mint(self, *args, **kwargs):
        mints.append(kwargs)
        return await original_mint(self, *args, **kwargs)

    async def authenticate(self, token):
        if token != "ops-token":
            return None
        return AgentIdentity(
            agent_id=AGENT,
            display_name="Ops agent",
            permissions=identity["permissions"],
            councils={COUNCIL},
        )

    async def no_stores(self):
        return None

    identity = {"permissions": {"council:petition", "tool:write"}}
    monkeypatch.setattr("nomos.nat.functions.create_backend", lambda *a, **k: backend)
    monkeypatch.setattr("nomos.llm.council_agent.create_backend", lambda *a, **k: backend)
    monkeypatch.setattr(OPAHttpClient, "evaluate", evaluate)
    monkeypatch.setattr(WritFactory, "mint_writ", mint)
    monkeypatch.setattr(AgentAuthenticator, "authenticate", authenticate)
    monkeypatch.setattr(CouncilService, "init_stores", no_stores)
    monkeypatch.setattr(CouncilService, "init_redis", no_stores)

    config = tmp_path / "key-ops.yaml"
    config.write_text(
        COUNCIL_YAML + f"council_key_path: {json.dumps(str(tmp_path / 'keys'))}\n",
        encoding="utf-8",
    )
    service = CouncilService(load_config(str(config)))
    service._authenticator = AgentAuthenticator(None)
    service._durable_store = DurableStub()

    return type(
        "Council",
        (),
        {
            "service": service,
            "backend": backend,
            "opa_inputs": opa_inputs,
            "mints": mints,
            "identity": identity,
        },
    )()


def request(tool_name=TOOL, arguments=None, **extra):
    return AuthorizeInput.model_validate(
        {
            "agent_id": AGENT,
            "auth_token": "ops-token",
            "council_id": COUNCIL,
            "description": "Rotate the billing-api production key",
            "justification": "The user asked to rotate the billing-api production key.",
            "target_resource": "secrets/billing-api/production",
            "tool_call": {
                "schema_version": 1,
                "tool_name": tool_name,
                "arguments": GOOD_ARGS if arguments is None else arguments,
                "subject": "tenant-acme:user-42",
            },
            **extra,
        }
    )


def trusted_system_payload(messages):
    """The operator catalog system message, decoded; asserts there is exactly one."""
    marker = "Operator-configured trusted tool definitions for this council."
    found = [m for m in messages if m.role == LLMRole.SYSTEM and m.content.startswith(marker)]
    assert len(found) == 1
    return json.loads(found[0].content.split("\n", 1)[1])


def user_prompt(messages):
    users = [m for m in messages if m.role == LLMRole.USER]
    assert len(users) == 1
    return users[0].content


# ── (a) a well-formed call ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_well_formed_call_is_resolved_validated_and_reviewed_against_operator_contract(
    council,
):
    result = await council.service.authorize(request())

    assert result.status == "approved", result.error
    assert result.writ

    # OPA sees the operator's definition and the exact arguments.
    (opa,) = council.opa_inputs
    assert opa["council"]["tools"] == [CATALOG_DEFINITION]
    assert opa["intent"]["tool_definition"] == CATALOG_DEFINITION
    assert opa["intent"]["tool_arguments"] == GOOD_ARGS
    assert opa["required_permissions"] == ["tool:write"]

    # Both members review; each gets the operator contract as a separate system
    # message and the live call as untrusted data in the user prompt.
    assert len(council.backend.calls) == 2
    for messages in council.backend.calls:
        trusted = trusted_system_payload(messages)
        assert trusted["council_id"] == COUNCIL
        assert trusted["matched_tool"] == CATALOG_DEFINITION
        assert trusted["tools"] == [CATALOG_DEFINITION]
        prompt = user_prompt(messages)
        assert "## Proposed Tool Operation" in prompt
        assert '"grace_period_minutes":15' in prompt

    # The writ binds the call by name, subject and argument digest.
    (minted,) = council.mints
    operation = minted["intent"].operation
    assert operation.tool_name == TOOL
    assert operation.subject == "tenant-acme:user-42"


# ── (b) arguments that violate the schema ──────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {**GOOD_ARGS, "environment": "production-eu"},  # not in the enum
        {**GOOD_ARGS, "grace_period_minutes": 0},  # below minimum
        {**GOOD_ARGS, "grace_period_minutes": "15"},  # wrong type; no coercion
        {"service": "billing-api", "environment": "production"},  # missing required
        {**GOOD_ARGS, "revoke_now": True},  # additionalProperties: false
    ],
    ids=["enum", "minimum", "type", "required", "extra"],
)
async def test_schema_violation_is_refused_before_opa_review_or_writ(council, arguments):
    result = await council.service.authorize(request(arguments=arguments))

    assert result.status == "error"
    assert result.error == "Invalid tool call: invalid tool catalog or arguments"
    assert not result.writ
    assert council.opa_inputs == []
    assert council.backend.calls == []
    assert council.mints == []


# ── (c) a tool the catalog does not define ─────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["secrets.delete_api_key", "Secrets.Rotate_API_Key"])
async def test_tool_outside_catalog_fails_closed_with_no_fallback(council, tool_name):
    result = await council.service.authorize(request(tool_name=tool_name))

    assert result.status == "error"
    assert result.error == "Invalid tool call: tool name is not in this council's catalog"
    assert not result.writ
    assert council.opa_inputs == []
    assert council.backend.calls == []
    assert council.mints == []


@pytest.mark.asyncio
async def test_catalog_does_not_map_tools_to_permissions_beyond_tool_write(council):
    """Every structured call needs tool:write; the catalog adds no per-tool scope."""
    council.identity["permissions"] = {"council:petition"}

    result = await council.service.authorize(request())

    assert result.status == "denied"
    assert result.missing_permissions == ["tool:write"]
    assert council.backend.calls == [] and council.mints == []


# ── (d) Relay certified tool definitions alongside the catalog ─────────────


def relay_signer():
    """PluginSigner from the Relay package, without executing its runtime-bound __init__."""
    name = "_nomos13_e2e_relay"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, RELAY_PACKAGE / "__init__.py", submodule_search_locations=[str(RELAY_PACKAGE)]
        )
        sys.modules[name] = importlib.util.module_from_spec(spec)
    return importlib.import_module(f"{name}.signing").PluginSigner(b"\x07" * 32)


def observed_tool_definitions(signer, called):
    """The item NomosPlugin._tool_definitions builds (plugin.py), signed the same way."""
    tools = [called] if called is not None else []
    observed = {
        "type": "nomos.observed_tool_definitions",
        "version": 1,
        "agent_id": AGENT,
        "council_id": COUNCIL,
        "tool_call_id": "call-1",
        "tool": TOOL,
        "tools_sha256": hashlib.sha256(
            json.dumps(tools, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "signed_at": time.time(),
        "tool_count": len(tools),
        "called": called,
    }
    return signer.certify(
        json.dumps(observed, separators=(",", ":"), allow_nan=False),
        command=RELAY_TOOL_DEFINITIONS_COMMAND,
        agent_id=AGENT,
        council_id=COUNCIL,
        request_id="call-1",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("called", [RELAY_OFFERED, None], ids=["disagreeing", "not_offered"])
async def test_relay_observed_definitions_are_rendered_as_evidence_and_never_compared(
    council, called
):
    """Pins current behavior: Relay's item is verified and shown; nothing compares it.

    ``disagreeing``: the model was offered a broader contract under the same
    name. ``not_offered``: Relay certifies the model was not offered the tool
    it called at all. Both are approved today, exactly as without the item.
    """
    signer = relay_signer()
    council.service._durable_store.trusted[signer.key_fingerprint] = {
        "executor_id": signer.executor_id,
        "public_key_hex": signer.public_key_hex,
        "key_fingerprint": signer.key_fingerprint,
        "enabled": "1",
    }
    item = observed_tool_definitions(signer, called)

    result = await council.service.authorize(request(certified_evidence=[item]))

    # No comparison and no rejection: the decision is the catalog path's.
    assert result.status == "approved", result.error
    assert result.writ

    # OPA receives the catalog only; certified evidence never reaches OPA input.
    (opa,) = council.opa_inputs
    assert opa["intent"]["tool_definition"] == CATALOG_DEFINITION
    assert RELAY_OFFERED_DESCRIPTION not in json.dumps(opa)
    assert "observed_tool_definitions" not in json.dumps(opa)

    for messages in council.backend.calls:
        # The operator contract stays the trusted system context.
        assert trusted_system_payload(messages)["matched_tool"] == CATALOG_DEFINITION
        system_text = "".join(m.content for m in messages if m.role == LLMRole.SYSTEM)
        assert "observed_tool_definitions" not in system_text
        # Relay's observation is generic certified evidence in the user prompt,
        # with no label tying it to the catalog entry it disagrees with.
        prompt = user_prompt(messages)
        assert "CERTIFIED EXECUTOR EVIDENCE" in prompt
        assert '"type":"nomos.observed_tool_definitions"' in prompt
        assert f"executor_id: {signer.executor_id}" in prompt
        assert (RELAY_OFFERED_DESCRIPTION in prompt) is (called is not None)
        if called is None:
            assert '"called":null' in prompt
