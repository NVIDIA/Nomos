# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""NOMOS Tier-1b: 1-to-1 ledger writes.

A reviewed intent must produce exactly ONE consolidated `INTENT_REVIEW_RECORDED`
entry instead of the ~7 intermediate review entries (submitted, evaluated, vote
opened, votes, decision). Behavior is flag-gated: with
``consolidated_review_audit`` OFF (default) the legacy intermediate entries are
written and no consolidated entry appears; with it ON the intermediate review
entries are suppressed.

Council-level tests prove the suppression directly; the payload-builder tests pin
the schema + redaction contract.
"""

from __future__ import annotations

import asyncio
import json

import pytest
import yaml

from nomos.config.council_catalog_store import CouncilCatalogStore
from nomos.config.settings import load_council_catalog
from nomos.level_0.types import AgentRole, CouncilMember, Intent
from nomos.level_1.ledger import LedgerEntryType
from nomos.level_3.executor import NoOpHandler
from nomos.level_4.council import CouncilConfig, SecurityCouncil
from nomos.nat.functions import AuthorizeInput, ProposeIntentInput
from nomos.nat.registry import CouncilRegistry
from nomos.nat.review_audit import build_intent_review_ledger_payload

# The five review entry types Tier-1b collapses into one consolidated record.
_REVIEW_ENTRY_TYPES = {
    LedgerEntryType.INTENT_SUBMITTED,
    LedgerEntryType.INTENT_EVALUATED,
    LedgerEntryType.QUORUM_VOTE,
    LedgerEntryType.DECISION_MADE,
    LedgerEntryType.DEBATE_VOTE,
}


def _council(*, consolidated: bool) -> SecurityCouncil:
    council = SecurityCouncil(
        config=CouncilConfig(
            council_id="test-council",
            require_justification=True,
            consolidated_review_audit=consolidated,
        )
    )
    council.register_action_handler(NoOpHandler())
    council.register_agent("agent-1", AgentRole.APPLICATION)
    for mid, key in (("alice", "abc"), ("bob", "def")):
        council.register_council_member(
            CouncilMember(member_id=mid, public_key_hex=key, display_name=mid, weight=1)
        )
    return council


def _entries(council: SecurityCouncil):
    return [council.ledger.get(i) for i in range(council.ledger.length())]


def _entry_types(council: SecurityCouncil) -> list[LedgerEntryType]:
    return [e.entry_type for e in _entries(council)]


def _drive_review(council: SecurityCouncil) -> Intent:
    intent = Intent.create(
        agent_id="agent-1",
        action_type="system.noop",
        description="No-op action for 1-to-1 ledger test",
        target_resource="/tmp/test",
        justification="Testing consolidated review audit",
    )
    assert council.submit_intent(intent).requires_approval
    council.approve_intent(intent.intent_id, "alice")
    council.approve_intent(intent.intent_id, "bob")
    return intent


def test_legacy_mode_writes_intermediate_review_entries():
    council = _council(consolidated=False)
    _drive_review(council)
    types = _entry_types(council)
    # Legacy: the intermediate review entries are present...
    assert LedgerEntryType.INTENT_SUBMITTED in types
    assert LedgerEntryType.INTENT_EVALUATED in types
    assert LedgerEntryType.QUORUM_VOTE in types
    assert LedgerEntryType.DECISION_MADE in types
    # ...and the consolidated record is NOT (it is written by the orchestration).
    assert LedgerEntryType.INTENT_REVIEW_RECORDED not in types


def test_consolidated_mode_suppresses_intermediate_review_entries():
    council = _council(consolidated=True)
    _drive_review(council)
    types = _entry_types(council)
    # No intermediate review entry survives in consolidated mode.
    assert not (_REVIEW_ENTRY_TYPES & set(types)), [t.value for t in types]


def test_consolidated_mode_preserves_token_mint_entry():
    # Only the *review* entries collapse; capability-mint stays as its own entry.
    council = _council(consolidated=True)
    intent = _drive_review(council)
    council.execute_intent(intent.intent_id)
    types = _entry_types(council)
    assert LedgerEntryType.TOKEN_MINTED in types
    assert not (_REVIEW_ENTRY_TYPES & set(types))


def test_payload_builder_minimal_deny_has_empty_sections():
    payload = build_intent_review_ledger_payload(
        request_type="propose",
        intent_dict={"intent_id": "intent-abc", "command": "/bin/ls"},
        council_id="council-prod",
        agent_id="agent-1",
        final_state="denied",
        decision="deny",
        denial_reason="Denied by OPA policy",
        opa={"enabled": True, "allow": False, "reason": "blocked"},
    )
    assert payload["schema_version"] == 2
    assert payload["request_type"] == "propose"
    assert payload["intent_id"] == "intent-abc"
    assert payload["final"]["decision"] == "deny"
    assert payload["final"]["denial_reason"] == "Denied by OPA policy"
    assert payload["opa"]["allow"] is False
    # deny-before-review → no votes recorded
    assert payload["quorum"] == {}
    assert payload["debate_rounds"] == []


def test_payload_builder_defensively_redacts_context_without_mutating_input():
    intent_dict = {
        "intent_id": "i",
        "command": "/bin/echo hi",
        "context": "PRIVATE-CONTEXT-SENTINEL",
    }
    payload = build_intent_review_ledger_payload(
        request_type="authorize",
        intent_dict=intent_dict,
        council_id="c",
        agent_id="a",
        final_state="approved",
        decision="approve",
    )
    assert payload["intent"]["context"] == ""
    assert intent_dict["context"] == "PRIVATE-CONTEXT-SENTINEL"
    assert payload["redactions"] == {"intent_context": "redacted"}
    assert "PRIVATE-CONTEXT-SENTINEL" not in json.dumps(payload)
    assert set(payload) == {
        "schema_version",
        "request_type",
        "intent_id",
        "council_id",
        "agent_id",
        "intent",
        "redactions",
        "opa",
        "policy_evaluation",
        "chairman",
        "code_review",
        "quorum",
        "debate_rounds",
        "final",
        "timing",
    }


def test_legacy_intent_submitted_entry_redacts_context():
    council = _council(consolidated=False)
    intent = Intent.create(
        agent_id="agent-1",
        action_type="system.noop",
        description="No-op action with private review context",
        target_resource="/tmp/test",
        justification="Testing legacy ledger redaction",
        context="PRIVATE-LEGACY-CONTEXT",
    )

    assert council.submit_intent(intent).requires_approval
    submitted = next(
        entry for entry in _entries(council) if entry.entry_type == LedgerEntryType.INTENT_SUBMITTED
    )
    assert submitted.payload["context"] == ""
    assert "PRIVATE-LEGACY-CONTEXT" not in json.dumps(submitted.payload)
    assert intent.context == "PRIVATE-LEGACY-CONTEXT"


def test_payload_builder_carries_debate_rounds():
    rounds = [
        {"round_number": 0, "votes": [{"member_id": "alice", "decision": "approve"}]},
        {"round_number": 1, "votes": [{"member_id": "alice", "decision": "deny"}]},
    ]
    payload = build_intent_review_ledger_payload(
        request_type="authorize",
        intent_dict={"intent_id": "i"},
        council_id="c",
        agent_id="a",
        final_state="denied",
        decision="deny",
        debate_rounds=rounds,
    )
    assert len(payload["debate_rounds"]) == 2
    assert payload["debate_rounds"][1]["round_number"] == 1


def test_is_unique_violation_detection():
    from nomos.nat.functions import _is_unique_violation

    class _SqlStateError(Exception):
        sqlstate = "23505"

    class UniqueViolation(Exception):  # noqa: N818 - mimics psycopg's class name exactly
        pass

    assert _is_unique_violation(_SqlStateError("dup"))  # matched by sqlstate
    assert _is_unique_violation(UniqueViolation())  # matched by class name
    wrapped = RuntimeError("ledger append failed")
    wrapped.__cause__ = _SqlStateError("dup")
    assert _is_unique_violation(wrapped)  # walks the cause chain
    assert not _is_unique_violation(ValueError("unrelated"))


# ── orchestration-level: exactly one consolidated record per request ──────────


def _deployed_profile(council_id: str) -> dict:
    return {
        "council_id": council_id,
        "enabled": True,
        "require_auth": False,
        "require_council_membership": False,
        "require_justification": True,
        "llm_backends": {},
        "members": [],
        "opa": {"enabled": False},
        "council_key_path": "",
        "ledger_path": "",
        "redis": {"host": ""},
        "metadata": {},
    }


def _registry(tmp_path, monkeypatch) -> CouncilRegistry:
    catalog_path = tmp_path / "catalog.yaml"
    profile_dir = tmp_path / "councils"
    profile_dir.mkdir()
    (profile_dir / "council-alpha.yaml").write_text(
        yaml.safe_dump(_deployed_profile("council-alpha"))
    )
    catalog_path.write_text(
        yaml.safe_dump(
            {
                "default_council_id": "council-alpha",
                "councils": {"council-alpha": {"config_file": "councils/council-alpha.yaml"}},
            }
        )
    )
    monkeypatch.setenv("COUNCIL_DYNAMIC_LEDGER_DIR", str(tmp_path / "ledger"))
    store = CouncilCatalogStore(catalog_path=catalog_path, profile_dir=profile_dir)
    return CouncilRegistry(load_council_catalog(str(store.catalog_path)), store.catalog_path)


def _propose_input() -> ProposeIntentInput:
    return ProposeIntentInput(
        auth_token="",
        agent_id="agent-1",
        council_id="council-alpha",
        description="Read workspace state before continuing",
        target_resource="/workspace",
        justification="inspect files",
    )


def _authorize_input() -> AuthorizeInput:
    return AuthorizeInput(
        auth_token="",
        agent_id="agent-1",
        council_id="council-alpha",
        command="/bin/ls /workspace",
        working_directory="/workspace",
        description="Read workspace state before continuing",
        target_resource="/workspace",
        justification="inspect files",
    )


@pytest.mark.asyncio
async def test_propose_writes_exactly_one_consolidated_record(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    service, request = await registry.service_for_propose(_propose_input())
    # A no-members council always resolves (auto-approve or finalize-deny), never
    # stuck AWAITING, so the orchestration appends exactly one review record.
    await service.propose_intent(request)
    types = _entry_types(service.council)
    assert types.count(LedgerEntryType.INTENT_REVIEW_RECORDED) == 1
    assert not (_REVIEW_ENTRY_TYPES & set(types)), [t.value for t in types]


@pytest.mark.asyncio
async def test_authorize_writes_exactly_one_consolidated_record(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    service, request = await registry.service_for_authorize(_authorize_input())
    await service.authorize(request)
    types = _entry_types(service.council)
    assert types.count(LedgerEntryType.INTENT_REVIEW_RECORDED) == 1
    assert not (_REVIEW_ENTRY_TYPES & set(types)), [t.value for t in types]


@pytest.mark.asyncio
async def test_concurrent_proposals_one_record_each(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    service, _ = await registry.service_for_propose(_propose_input())
    # 16 concurrent proposals → 16 distinct intent_ids → 16 review records, no dupes.
    await asyncio.gather(*(service.propose_intent(_propose_input()) for _ in range(16)))
    types = _entry_types(service.council)
    assert types.count(LedgerEntryType.INTENT_REVIEW_RECORDED) == 16
    assert not (_REVIEW_ENTRY_TYPES & set(types)), [t.value for t in types]


@pytest.mark.asyncio
async def test_authorize_orchestration_carries_debate_into_record(tmp_path, monkeypatch):
    # Regression for the authorize path that discarded debate_history (codex review):
    # the consolidated record for an authorize that debated must include the rounds.
    from nomos.level_4.council.workflow import WorkflowState
    from nomos.nat.functions import DebateRound, MemberVote

    registry = _registry(tmp_path, monkeypatch)
    service, request = await registry.service_for_authorize(_authorize_input())

    debate = [
        DebateRound(
            round_number=0,
            votes=[MemberVote(member_id="alice", decision="approve", reasoning="r0")],
        ),
        DebateRound(
            round_number=1,
            votes=[MemberVote(member_id="alice", decision="deny", reasoning="r1")],
        ),
    ]

    async def fake_collect(intent, **kwargs):
        return debate[-1].votes, debate, 1, WorkflowState.DENIED

    monkeypatch.setattr(service, "_collect_review_outcome", fake_collect)
    await service.authorize(request)

    reviews = [
        e
        for e in _entries(service.council)
        if e.entry_type == LedgerEntryType.INTENT_REVIEW_RECORDED
    ]
    assert len(reviews) == 1
    assert len(reviews[-1].payload["debate_rounds"]) == 2


@pytest.mark.asyncio
async def test_file_mode_appends_twice_on_forced_duplicate(tmp_path, monkeypatch):
    # Honest documented behavior: file/dev mode has NO unique index, so a forced
    # replay of the same intent_id appends twice. Production (PG) dedupes via the
    # 0004 index + the _is_unique_violation catch (no-op). intent_id is a random
    # uuid, so this is unreachable in normal use.
    registry = _registry(tmp_path, monkeypatch)
    service, _ = await registry.service_for_propose(_propose_input())
    intent = Intent.create(
        agent_id="agent-1",
        action_type="system.noop",
        description="forced duplicate test",
        target_resource="/tmp/test",
        justification="dup",
    )
    for _ in range(2):
        service._record_consolidated_review(
            intent, request_type="propose", final_state="denied", decision="deny"
        )
    reviews = [
        e
        for e in _entries(service.council)
        if e.entry_type == LedgerEntryType.INTENT_REVIEW_RECORDED
    ]
    assert len(reviews) == 2
