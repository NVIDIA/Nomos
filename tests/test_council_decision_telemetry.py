# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The telemetry record the council publishes for each request.

Behind the gateway this record is the monitor's only view of the council, so
it has to say who asked, what the council decided, and why a request was
turned away, including the ones turned away before an intent existed.
"""

from typing import Any

import pytest

from nomos.auth.authenticator import AgentIdentity
from nomos.auth.permissions import PermissionScope
from nomos.config.settings import ChairmanConfig, CouncilSettings, OPAConfig
from nomos.level_0.types import CouncilMember, Intent
from nomos.llm.council_agent import ChairmanDecision, IntentEvaluation
from nomos.nat.functions import AuthorizeInput, CouncilService, ProposeIntentInput

_NO_SIGNER_KEY_PATH = "/nonexistent/nomos-decision-telemetry-tests/keys"


class _Member:
    provider_name = "fake"

    def __init__(self, member_id: str, decision: str):
        self.member_id = member_id
        self.display_name = member_id
        self._decision = decision

    async def evaluate_intent(self, _intent: Intent) -> IntentEvaluation:
        return IntentEvaluation(decision=self._decision, reasoning="test")


class _Chairman:
    provider_name = "fake"

    async def decide_threshold(self, _intent: Intent) -> ChairmanDecision:
        return ChairmanDecision(threshold="unanimous", risk_level="high")


def _service(*, require_auth: bool = False, votes: tuple[str, ...] = ()) -> CouncilService:
    service = CouncilService(
        CouncilSettings(
            council_id="telemetry-test",
            require_auth=require_auth,
            require_justification=True,
            llm_backends={},
            members=[],
            chairman=ChairmanConfig(parallel_member_evaluation=True),
            opa=OPAConfig(enabled=False),
            council_key_path=_NO_SIGNER_KEY_PATH,
        )
    )
    for index, decision in enumerate(votes):
        member_id = f"m{index}"
        service._agents.append(_Member(member_id, decision))  # type: ignore[arg-type]
        service.council.register_council_member(
            CouncilMember(member_id=member_id, public_key_hex="", display_name=member_id, weight=1)
        )
    if votes:
        service._chairman = _Chairman()  # type: ignore[assignment]
    return service


def _records(service: CouncilService) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    service._telemetry.publish_later = records.append  # type: ignore[method-assign]
    return records


def _proposal(**overrides: Any) -> ProposeIntentInput:
    fields: dict[str, Any] = {
        "agent_id": "agent-1",
        "command": "/bin/ls /tmp",
        "working_directory": "/tmp",
        "description": "Inspect temporary state for the telemetry test",
        "target_resource": "/tmp",
        "justification": "Check what the council publishes",
    }
    return ProposeIntentInput(**{**fields, **overrides})


def _authorization(**overrides: Any) -> AuthorizeInput:
    return AuthorizeInput(**{**_proposal().model_dump(), **overrides})


@pytest.mark.asyncio
async def test_a_reviewed_request_publishes_who_asked_and_how_the_council_voted():
    service = _service(votes=("approve", "deny", "deny"))
    records = _records(service)

    result = await service.propose_intent(_proposal())

    assert len(records) == 1
    record = records[0]
    assert record["intent_id"] == result.intent_id
    assert record["council_id"] == "telemetry-test"
    assert record["outcome"] == "denied"
    assert record["error_class"] == "council"
    # Auth is off in this council: the caller is named but was not authenticated.
    assert (record["agent_id"], record["agent_authenticated"]) == ("agent-1", False)
    assert record["decision"] == {
        "risk_level": "high",
        "threshold": "unanimous",
        "approval_votes": 1,
        "denial_votes": 2,
        "abstain_votes": 0,
        "changed_votes": 0,
        "debate_rounds": 0,
        "code_review": "",
        "missing_permissions": [],
        "guidance_count": 0,
        "writ_id": "",
    }


@pytest.mark.asyncio
async def test_an_authenticated_caller_is_named_by_identity_not_by_what_it_claimed(monkeypatch):
    service = _service(votes=("approve",))
    records = _records(service)
    identity = AgentIdentity(
        agent_id="real-agent",
        display_name="Real",
        permissions={permission.value for permission in PermissionScope},
        councils={service.council_id},
    )

    async def authenticate(_token: str) -> AgentIdentity:
        return identity

    monkeypatch.setattr(service, "_authenticate", authenticate)

    await service.propose_intent(_proposal(agent_id="someone-else", auth_token="t"))

    assert (records[0]["agent_id"], records[0]["agent_authenticated"]) == ("real-agent", True)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["authorize", "propose_intent"])
async def test_a_request_refused_at_authentication_is_still_published(tool):
    service = _service(require_auth=True)
    records = _records(service)
    request = _authorization() if tool == "authorize" else _proposal()

    result = await getattr(service, tool)(request)

    assert "Authentication" in result.error
    assert len(records) == 1
    record = records[0]
    assert record["tool"] == f"council_{tool}"
    assert (record["intent_id"], record["outcome"], record["error_class"]) == ("", "error", "auth")
    assert (record["agent_id"], record["agent_authenticated"]) == ("agent-1", False)
    assert set(record["stages"]) == {"auth", "total"}
    assert record["decision"]["approval_votes"] == 0


@pytest.mark.asyncio
async def test_load_shedding_is_published_and_does_not_feed_its_own_estimate(monkeypatch):
    service = _service()
    records = _records(service)
    service._intent_latency_ewma_s = 12.0
    monkeypatch.setattr(
        service, "_overload_rejection", lambda: ("council_overloaded: test", ["retry later"])
    )

    result = await service.authorize(_authorization())

    assert result.retryable is True
    assert (records[0]["outcome"], records[0]["error_class"]) == ("error", "overload")
    assert records[0]["limiters"] == [limiter.gauge() for limiter in service._limiters]
    # A shed request returns in about a millisecond. Had it counted, the average
    # response time that shedding projects from would fall and shedding would stop.
    assert service._intent_latency_ewma_s == 12.0


@pytest.mark.asyncio
async def test_a_malformed_request_is_published_as_a_validation_error():
    service = _service()
    records = _records(service)

    await service.propose_intent(_proposal(api_version="v2", council_id=""))

    assert (records[0]["outcome"], records[0]["error_class"]) == ("error", "validation")


@pytest.mark.asyncio
async def test_missing_permissions_are_published_by_name(monkeypatch):
    service = _service(votes=("approve",))
    records = _records(service)
    identity = AgentIdentity(
        agent_id="limited", display_name="Limited", permissions=set(), councils={service.council_id}
    )

    async def authenticate(_token: str) -> AgentIdentity:
        return identity

    monkeypatch.setattr(service, "_authenticate", authenticate)

    result = await service.authorize(_authorization(auth_token="t"))

    assert result.missing_permissions
    assert records[0]["error_class"] == "permission"
    assert records[0]["decision"]["missing_permissions"] == result.missing_permissions
