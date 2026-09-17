# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""SecurityCouncil write-through + cross-replica rehydrate.

The deployed council runs a whole review (submit → in-process votes → debate →
decide → mint) atomically within one request on one replica, so the only
multi-replica gap is *reads* of an already-decided workflow (check_intent_status
/ get_pending_approvals) load-balancing to a replica other than the decider.

These tests use an in-memory fake store (same duck-typed surface as
``PostgresWorkflowStore``) to prove the contract without a database:
- the deciding replica writes the workflow through on terminal/decision transitions;
- a *fresh* council backed by the same store rehydrates it on a local-cache miss;
- the store path is best-effort — a store outage never breaks a committed decision;
- a council with no store behaves byte-identically to before (file/redis mode).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from nomos.level_0.types import AgentRole, CouncilMember, Intent
from nomos.level_3.executor import NoOpHandler
from nomos.level_4.council import CouncilConfig, SecurityCouncil, WorkflowState
from nomos.level_4.council.workflow import IntentWorkflow


class FakeWorkflowStore:
    """In-memory stand-in for PostgresWorkflowStore (identical duck-typed API)."""

    def __init__(self, *, fail: bool = False):
        self.rows: dict[str, dict[str, Any]] = {}
        self.fail = fail
        self.upserts = 0
        self.reads = 0
        self.reaps = 0

    def upsert_workflow(self, intent_id, workflow_json, *, state, is_terminal, expires_at):
        self.upserts += 1
        if self.fail:
            raise RuntimeError("store down")
        existing = self.rows.get(intent_id)
        version = (existing["version"] + 1) if existing else 1
        self.rows[intent_id] = {
            "workflow_json": workflow_json,
            "state": state,
            "is_terminal": is_terminal,
            "expires_at": expires_at,
            "version": version,
        }

    def get_workflow(self, intent_id):
        self.reads += 1
        if self.fail:
            raise RuntimeError("store down")
        row = self.rows.get(intent_id)
        return row["workflow_json"] if row else None

    def list_workflows_by_state(self, states):
        if self.fail:
            raise RuntimeError("store down")
        wanted = set(states)
        return [r["workflow_json"] for r in self.rows.values() if r["state"] in wanted]

    def reap_terminal_before(self, cutoff):
        self.reaps += 1
        removed = [
            iid
            for iid, r in self.rows.items()
            if r["is_terminal"] and r["expires_at"] is not None and r["expires_at"] < cutoff
        ]
        for iid in removed:
            del self.rows[iid]
        return len(removed)


def _council(store=None) -> SecurityCouncil:
    config = CouncilConfig(
        council_id="test-council",
        require_justification=True,
        default_token_ttl_seconds=300,
    )
    council = SecurityCouncil(config=config, workflow_store=store)
    council.register_action_handler(NoOpHandler())
    council.register_agent("agent-1", AgentRole.APPLICATION)
    for mid, key in (("alice", "abc"), ("bob", "def")):
        council.register_council_member(
            CouncilMember(member_id=mid, public_key_hex=key, display_name=mid, weight=1)
        )
    return council


def _noop_intent(tag: str = "1") -> Intent:
    return Intent.create(
        agent_id="agent-1",
        action_type="system.noop",
        description=f"No-op action {tag} for testing",
        target_resource="/tmp/test",
        justification="workflow store test",
    )


def _drive_to_completed(council: SecurityCouncil, intent: Intent) -> IntentWorkflow:
    submit = council.submit_intent(intent)
    assert submit.success, submit.error
    assert submit.requires_approval
    council.approve_intent(intent.intent_id, "alice")
    council.approve_intent(intent.intent_id, "bob")
    execute = council.execute_intent(intent.intent_id)
    assert execute.success, execute.error
    return execute.workflow


def test_decision_written_through_and_rehydrated_on_fresh_replica():
    store = FakeWorkflowStore()
    intent = _noop_intent()
    wf = _drive_to_completed(_council(store), intent)
    assert wf.state == WorkflowState.COMPLETED

    # The decision landed in the shared store...
    assert store.rows[intent.intent_id]["state"] == "completed"
    assert store.rows[intent.intent_id]["is_terminal"] is True

    # ...so a DIFFERENT replica (fresh council, empty local cache) answers a
    # status query by rehydrating from the store rather than "Intent not found".
    other_replica = _council(store)
    rehydrated = other_replica.get_workflow(intent.intent_id)
    assert rehydrated is not None
    assert rehydrated.state == WorkflowState.COMPLETED
    assert rehydrated.intent_id == intent.intent_id


def test_policy_denied_intent_persisted_and_rehydrated():
    store = FakeWorkflowStore()
    intent = _noop_intent("deny")
    # The deny-anonymous policy rule gives a deterministic terminal DENY in submit.
    result = _council(store).submit_intent(intent, agent_role=AgentRole.ANONYMOUS)
    assert result.success is False
    assert result.workflow is not None
    assert result.workflow.state == WorkflowState.DENIED

    rehydrated = _council(store).get_workflow(intent.intent_id)
    assert rehydrated is not None
    assert rehydrated.state == WorkflowState.DENIED
    assert rehydrated.is_terminal


def test_no_store_behaves_identically():
    council = _council(store=None)
    intent = _noop_intent("nostore")
    wf = _drive_to_completed(council, intent)
    assert wf.state == WorkflowState.COMPLETED
    # Local cache still answers; no store, no rehydrate, no error.
    assert council.get_workflow(intent.intent_id).state == WorkflowState.COMPLETED
    assert council.get_workflow("nonexistent") is None


def test_read_through_miss_returns_none_but_consults_store():
    store = FakeWorkflowStore()
    council = _council(store)
    assert council.get_workflow("nonexistent") is None
    assert store.reads >= 1


def test_persist_failure_is_swallowed_decision_still_succeeds():
    # codex concern #2: a store outage must NOT fail the decision — it is already
    # committed to the ledger. The request still succeeds; only cross-replica
    # status visibility lags until the store recovers.
    store = FakeWorkflowStore(fail=True)
    council = _council(store)
    intent = _noop_intent("failstore")
    wf = _drive_to_completed(council, intent)  # must not raise
    assert wf.state == WorkflowState.COMPLETED
    assert store.upserts > 0  # we attempted to persist
    assert store.rows == {}  # but nothing stuck


def test_get_pending_approvals_unions_store():
    store = FakeWorkflowStore()
    # An AWAITING workflow opened on another replica, visible only via the store.
    intent = _noop_intent("pending-elsewhere")
    wf = IntentWorkflow(
        intent=intent,
        state=WorkflowState.AWAITING_APPROVAL,
        submitted_at=datetime.now(UTC),
    )
    store.upsert_workflow(
        intent.intent_id,
        wf.to_durable_dict(),
        state=WorkflowState.AWAITING_APPROVAL.value,
        is_terminal=False,
        expires_at=None,
    )

    pending = _council(store).get_pending_approvals()  # fresh replica, no local copy
    assert intent.intent_id in [w.intent_id for w in pending]


def test_reap_only_removes_expired_terminal_rows():
    store = FakeWorkflowStore()
    now = datetime.now(UTC)
    # expired terminal → reaped; future terminal → kept; non-terminal → kept.
    store.upsert_workflow(
        "expired", {}, state="completed", is_terminal=True, expires_at=now - timedelta(hours=1)
    )
    store.upsert_workflow(
        "fresh", {}, state="completed", is_terminal=True, expires_at=now + timedelta(hours=1)
    )
    store.upsert_workflow(
        "pending", {}, state="awaiting_approval", is_terminal=False, expires_at=None
    )

    removed = store.reap_terminal_before(now)
    assert removed == 1
    assert set(store.rows) == {"fresh", "pending"}
