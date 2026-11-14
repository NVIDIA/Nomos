"""Pre-intent availability boundary: store transport failures become typed errors."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from psycopg import IntegrityError, InterfaceError, OperationalError
from psycopg_pool import PoolTimeout
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from nomos.auth.authenticator import AgentIdentity
from nomos.config.settings import CouncilSettings, OPAConfig
from nomos.nat.functions import AuthorizeInput, CouncilService, ProposeIntentInput


@pytest_asyncio.fixture
async def service(tmp_path):
    result = CouncilService(
        CouncilSettings(
            council_id="test",
            require_auth=True,
            members=[],
            llm_backends={},
            opa=OPAConfig(enabled=False),
            council_key_path=str(tmp_path / "keys"),
            ledger_path="",
        )
    )
    yield result
    await result.aclose()


def request(kind):
    if kind == "authorize":
        return AuthorizeInput(
            auth_token="synthetic-token",
            agent_id="app",
            command="/bin/true",
            working_directory="/workspace",
            justification="Read-only test",
        )
    return ProposeIntentInput(
        auth_token="synthetic-token",
        agent_id="app",
        description="Read configuration",
        target_resource="/workspace/config",
        justification="Read-only test",
    )


async def invoke(service, kind):
    return await (
        service.authorize(request(kind))
        if kind == "authorize"
        else service.propose_intent(request(kind))
    )


@pytest.mark.parametrize("kind", ["authorize", "propose"])
@pytest.mark.parametrize(
    "failure",
    [RedisConnectionError, RedisTimeoutError, OperationalError, InterfaceError, PoolTimeout],
)
@pytest.mark.asyncio
async def test_transport_unavailability_is_typed_before_any_intent(service, kind, failure):
    service._authenticator = SimpleNamespace(
        authenticate=AsyncMock(side_effect=failure("private dependency detail"))
    )
    service._prepare_request_evidence = AsyncMock(
        side_effect=AssertionError("must not reach evidence")
    )
    before = service._council.ledger._storage.length()
    output = await invoke(service, kind)
    assert (output.status == "error") if kind == "authorize" else (not output.success)
    assert output.error_code == "dependency_unavailable" and output.retryable
    assert "private" not in output.error
    assert not service._council._workflows
    assert service._council.ledger._storage.length() == before
    assert not service._prepare_request_evidence.called
    wire = output.model_dump(mode="json")
    assert wire["error_code"] == "dependency_unavailable" and wire["retryable"] is True
    if kind == "authorize":
        assert not output.writ and not output.writ_id and not output.decision_id
    else:
        assert not output.intent_id


@pytest.mark.parametrize("kind", ["authorize", "propose"])
@pytest.mark.asyncio
async def test_missing_authenticator_and_invalid_identity_keep_existing_contract(service, kind):
    unavailable = await invoke(service, kind)
    assert "Authentication service unavailable" in unavailable.error
    assert not unavailable.retryable and unavailable.error_code is None
    service._authenticator = SimpleNamespace(authenticate=AsyncMock(return_value=None))
    invalid = await invoke(service, kind)
    assert "Authentication failed or missing token" in invalid.error
    assert not invalid.retryable and invalid.error_code is None
    assert "retryable" not in invalid.model_dump() and "error_code" not in invalid.model_dump()


@pytest.mark.parametrize("failure", [asyncio.CancelledError, IntegrityError, ValueError])
@pytest.mark.asyncio
async def test_cancellation_integrity_and_programming_errors_are_not_relabelled(service, failure):
    service._authenticator = SimpleNamespace(
        authenticate=AsyncMock(side_effect=failure("sentinel"))
    )
    with pytest.raises(failure):
        await service.authorize(request("authorize"))
    assert not service._council._workflows


@pytest.mark.asyncio
async def test_membership_denial_preserved(service):
    service._settings.require_council_membership = True
    identity = AgentIdentity("app", "App", {"shell:execute"}, {"different"})
    service._authenticator = SimpleNamespace(authenticate=AsyncMock(return_value=identity))
    output = await service.authorize(request("authorize"))
    assert "not a member" in output.error
    assert not output.retryable and output.error_code is None
    assert not service._council._workflows


@pytest.mark.asyncio
async def test_non_auth_transport_exception_is_not_normalized(service):
    identity = AgentIdentity("app", "App", {"shell:execute"}, {"test"})
    service._authenticator = SimpleNamespace(authenticate=AsyncMock(return_value=identity))
    service._prepare_request_evidence = AsyncMock(side_effect=OperationalError("later phase"))
    with pytest.raises(OperationalError):
        await service.authorize(request("authorize"))


@pytest.mark.asyncio
async def test_real_hmac_rejection_remains_nonretryable(service):
    import hashlib

    from nomos.auth.account_integrity import sign_account_record, verify_account_record
    from nomos.auth.authenticator import AgentAuthenticator
    from nomos.store.base import AccountAuthSnapshot

    permissions, councils = {"shell:execute"}, {"test"}
    account = {"token_hash": hashlib.sha256(b"synthetic-token").hexdigest(), "enabled": "1"}
    account["_integrity_hmac"] = sign_account_record(
        key="synthetic-key",
        agent_id="app",
        account=account,
        permissions=permissions,
        councils=councils,
    )
    account["display_name"] = "tampered after signing"

    class Store:
        async def get_auth_snapshot(self, token_hash):
            return AccountAuthSnapshot("app", account, permissions, councils)

        def verify_account_integrity(self, agent_id, record, scopes, memberships):
            return verify_account_record(
                key="synthetic-key",
                agent_id=agent_id,
                account=record,
                permissions=scopes,
                councils=memberships,
            )

    ephemeral = SimpleNamespace(
        get_rate_limit_count=AsyncMock(return_value=0), increment_rate_limit=AsyncMock()
    )
    service._authenticator = AgentAuthenticator(Store(), ephemeral)
    output = await service.authorize(request("authorize"))
    assert "Authentication failed or missing token" in output.error
    assert not output.retryable and output.error_code is None
    assert not service._council._workflows and not ephemeral.increment_rate_limit.called


@pytest.mark.asyncio
async def test_rate_limit_rejection_keeps_existing_contract(service):
    from nomos.auth.authenticator import AgentAuthenticator

    store = SimpleNamespace(
        get_auth_snapshot=AsyncMock(side_effect=AssertionError("no account lookup"))
    )
    ephemeral = SimpleNamespace(get_rate_limit_count=AsyncMock(return_value=5))
    service._authenticator = AgentAuthenticator(store, ephemeral)
    output = await service.authorize(request("authorize"))
    assert not output.retryable and output.error_code is None
    assert "Authentication failed or missing token" in output.error
    assert not store.get_auth_snapshot.called


@pytest.mark.asyncio
async def test_missing_token_does_not_invoke_authenticator(service):
    authenticate = AsyncMock(side_effect=RedisConnectionError("must not call"))
    service._authenticator = SimpleNamespace(authenticate=authenticate)
    output = await service.authorize(request("authorize").model_copy(update={"auth_token": ""}))
    assert not output.retryable and output.error_code is None and not authenticate.called


@pytest.mark.asyncio
async def test_workflow_maintenance_starts_once_and_stops_with_the_service(service):
    entered = asyncio.Event()

    async def maintain():
        entered.set()
        await asyncio.Event().wait()

    service._maintain_workflows = maintain
    service.start_workflow_maintenance()
    task = service._workflow_janitor
    service.start_workflow_maintenance()
    assert service._workflow_janitor is task
    await entered.wait()
    assert not service._council._workflows
    await service.aclose()
    assert task.done()
    service.start_workflow_maintenance()
    assert service._workflow_janitor is task
