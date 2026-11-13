"""Audit idempotency must never turn an unrecorded approval into a writ."""

import copy
from types import SimpleNamespace

import pytest

from nomos.auth.writ_factory import WritFactory
from nomos.config.settings import CouncilSettings
from nomos.crypto.signer_provider import InMemorySigner
from nomos.level_0.crypto import KeyPair
from nomos.level_0.types import CouncilMember
from nomos.level_1.ledger import LedgerAppendError, LedgerEntryType
from nomos.llm.council_agent import IntentEvaluation
from nomos.nat.functions import AuthorizeInput, CouncilService
from tests.postgres_test_support import postgres_unavailable_reason
from tests.test_postgres_ledger import pool as pool

_PG_SKIP = postgres_unavailable_reason()


class ReviewConflictError(Exception):
    sqlstate = "23505"

    def __init__(self, constraint="ledger_entries_review_idem_idx"):
        self.diag = SimpleNamespace(constraint_name=constraint)


def _approving_service():
    service = CouncilService(
        CouncilSettings(
            council_id="probe",
            require_auth=False,
            require_council_membership=False,
            members=[],
            llm_backends={},
            council_key_path="",
            ledger_path="",
            storage={"mode": "redis"},
            signing={"provider": "local"},
            redis={"host": ""},
            opa={"enabled": False},
            chairman={"enabled": False},
            code_review={"enabled": False},
            telemetry={"enabled": False, "redis_enabled": False},
        )
    )
    signer = InMemorySigner(KeyPair.generate(key_id="probe"))
    service._signer_provider = signer
    service.council.ledger._signer = signer

    class Member:
        member_id = display_name = "member"
        provider_name = "offline"

        async def evaluate_intent(self, intent):
            return IntentEvaluation(decision="approve", reasoning="offline")

    service._agents = [Member()]
    service.council.register_council_member(CouncilMember("member", "", "Member"))
    return service


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["unrelated", "missing", "conflicting", "lookup_failure", "matching"]
)
async def test_authorize_requires_matching_committed_audit(mode, monkeypatch):
    service = _approving_service()
    ledger = service.council.ledger
    before = ledger.length()
    append = ledger.append
    minted = []
    original_mint = WritFactory.mint_writ

    async def record_mint(factory, *args, **kwargs):
        minted.append(True)
        return await original_mint(factory, *args, **kwargs)

    monkeypatch.setattr(WritFactory, "mint_writ", record_mint)

    def conflict(entry_type, payload, **kwargs):
        if entry_type != LedgerEntryType.INTENT_REVIEW_RECORDED:
            return append(entry_type, payload, **kwargs)
        if mode in ("matching", "conflicting"):
            stored = copy.deepcopy(payload)
            if mode == "conflicting":
                stored["final"]["decision"] = "deny"
            append(entry_type, stored, **kwargs)
        constraint = (
            "ledger_entries_pkey" if mode == "unrelated" else "ledger_entries_review_idem_idx"
        )
        raise ReviewConflictError(constraint)

    monkeypatch.setattr(ledger, "append", conflict)
    if mode == "lookup_failure":

        def unavailable(intent_id):
            raise RuntimeError("receipt lookup unavailable")

        monkeypatch.setattr(ledger, "get_review_by_intent", unavailable)

    request = AuthorizeInput(
        council_id="probe",
        agent_id="probe",
        command="pwd",
        working_directory="/workspace",
        description="offline review",
        target_resource="/workspace",
        justification="audit check",
    )
    try:
        if mode == "matching":
            result = await service.authorize(request)
            assert result.status == "approved" and result.writ
            assert len(minted) == 1
            entry = ledger.get_review_by_intent(result.decision_id)
            assert entry is not None and entry.payload["final"]["decision"] == "approve"
            workflow = service.council.get_workflow(result.decision_id)
            assert entry.sequence in workflow.ledger_sequences
        else:
            error = (
                ReviewConflictError
                if mode == "unrelated"
                else (RuntimeError if mode == "lookup_failure" else LedgerAppendError)
            )
            with pytest.raises(error):
                await service.authorize(request)
            assert not minted
        assert ledger.length() - before == (1 if mode in ("matching", "conflicting") else 0)
    finally:
        service._ledger_executor.shutdown(wait=True)


@pytest.mark.skipif(_PG_SKIP is not None, reason=_PG_SKIP or "")
def test_real_postgres_duplicate_requires_equal_payload_and_returns_receipt(pool):
    from tests.test_postgres_ledger import _chain

    ledger = _chain(pool)
    ledger.initialize("test")
    service = object.__new__(CouncilService)
    service._council = SimpleNamespace(ledger=ledger)
    payload = {"intent_id": "duplicate", "decision": "approve", "timing": {"total": 1}}
    first = service._append_review_entry(payload, "duplicate")
    assert service._append_review_entry(copy.deepcopy(payload), "duplicate") == first
    with pytest.raises(LedgerAppendError, match="matching committed"):
        service._append_review_entry({**payload, "decision": "deny"}, "duplicate")
    with pytest.raises(LedgerAppendError, match="matching committed"):
        service._append_review_entry({**payload, "timing": {"total": 2}}, "duplicate")
    assert ledger.length() == 2
    assert ledger.validate()


@pytest.mark.skipif(_PG_SKIP is not None, reason=_PG_SKIP or "")
def test_real_postgres_lost_commit_ack_reconciles_matching_receipt(pool, monkeypatch):
    import psycopg

    from tests.test_postgres_ledger import _chain

    ledger = _chain(pool)
    ledger.initialize("test")
    storage = ledger._storage
    original = storage._append_once
    calls = []

    def lost_reply(build):
        calls.append(True)
        entry = original(build)
        if len(calls) == 1:
            # The database committed but the caller could not observe the reply.
            raise psycopg.OperationalError("lost COMMIT reply")
        return entry

    monkeypatch.setattr(storage, "_append_once", lost_reply)
    service = object.__new__(CouncilService)
    service._council = SimpleNamespace(ledger=ledger)
    payload = {"intent_id": "unknown-commit", "decision": "approve"}
    assert service._append_review_entry(payload, "unknown-commit") == 1
    assert len(calls) == 2
    assert ledger.length() == 2
    assert ledger.get_review_by_intent("unknown-commit").payload == payload
    assert ledger.validate()
