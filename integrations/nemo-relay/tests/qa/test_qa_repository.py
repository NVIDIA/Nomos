# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""DESIGN.md `repository`: the in-memory EvidenceStore: conversations, turns, journal, records, claims, seed."""

from __future__ import annotations

import dataclasses
import json
import time

import pytest
import qa_support as qa

from nemo_relay_nomos import records
from nemo_relay_nomos.repository import EvidenceStore, StoreError
from nemo_relay_nomos.settings import Limits
from nemo_relay_nomos.signing import PluginSigner

SIGNER = PluginSigner(qa.PLUGIN_SEED)
OK_OUTCOME = {"status": "ok", "execution_status": "executed", "evidence_status": "plugin_signed"}


def _limits(**kw) -> Limits:
    return dataclasses.replace(Limits(), **kw)


def _record(*, kind="local_result", created_at=None, marker="QA_MARKER_ORIGINAL", call_id="call-x"):
    return records.plugin_record(
        evidence_id=qa.evidence_id(),
        kind=kind,
        tool_name="qa_local",
        tool_call_id=call_id,
        arguments={"path": "/workspace/a"},
        content={"output": marker},
        signer=SIGNER,
        now=created_at if created_at is not None else time.time(),
    )


def _invalid_executor_record():
    writ = qa.mint_writ("echo hi")
    return records.executor_record(
        evidence_id=qa.evidence_id(),
        tool_name="run_shell",
        tool_call_id="call-bad",
        arguments={"command": "echo hi"},
        result=qa.signed_result(writ),
        writ=writ,
        verified=False,
        verification_error="qa",
        receipt_signer=SIGNER,
        policy_version="qa-trust-1",
        now=time.time(),
    )


@pytest.fixture
def store():
    s = EvidenceStore(qa.OWNER, Limits())
    yield s
    s.close()


@pytest.fixture
def conv(store):
    """One conversation in ``store``, opened the way H1 opens one: a turn with no earlier tokens."""
    return store.open_turn([], "qa user request").conversation


def _assert_holds(got: dict, rec: dict, how: str):
    # The spec does not promise get() returns *only* the stored keys; it must return them unchanged.
    changed = {k: got.get(k) for k in rec if got.get(k) != rec[k]}
    assert not changed, f"get after {how} changed or lost record fields: {changed!r}"


def _code(exc_info, code):
    assert qa.error_code_matches(exc_info.value, code), (
        f"StoreError {exc_info.value!r} must carry code {code!r}"
    )


# ---- lifetime -------------------------------------------------------------------------------------


def test_a_new_store_starts_empty_and_touches_no_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = EvidenceStore(qa.OWNER, Limits())
    try:
        rec = _record()
        assert s.turn("nt_" + "0" * 32) is None, "a new store must know no turn"
        with pytest.raises(StoreError):
            s.get(rec["evidence_id"])
        s.put(rec)
        assert list(tmp_path.iterdir()) == [], "the in-memory store must not create files"
    finally:
        s.close()


def test_two_stores_never_share_records():
    first, second = EvidenceStore(qa.OWNER, Limits()), EvidenceStore(qa.OWNER, Limits())
    try:
        rec = _record()
        first.put(rec)
        with pytest.raises(StoreError) as info:
            second.get(rec["evidence_id"])
        _code(info, "evidence_not_found")
    finally:
        first.close()
        second.close()


# ---- turns and conversations ------------------------------------------------------------------------


def test_open_turn_without_known_tokens_starts_a_new_conversation(store):
    a = store.open_turn([], "first")
    b = store.open_turn(["nt_" + "0" * 32], "second")  # an unknown token names nothing
    assert a.token != b.token and a.conversation != b.conversation
    assert a.token.startswith("nt_") and len(a.token) == 35
    assert a.citable == frozenset({a.conversation}) and a.user_request == "first"
    assert store.turn(a.token) == a, f"turn({a.token}) must return the turn opened"


def test_open_turn_with_a_known_token_continues_that_conversation(store):
    a = store.open_turn([], "first")
    b = store.open_turn([a.token], "second")
    assert b.conversation == a.conversation and b.token != a.token
    assert b.citable == frozenset({a.conversation}) and store.turn(b.token).user_request == "second"
    assert store.turn(a.token).user_request == "first", "an earlier turn keeps its own user request"


def test_open_turn_over_several_conversations_starts_one_with_the_strictest_state(store):
    a, b = store.open_turn([], "a"), store.open_turn([], "b")
    store.set_denials(a.conversation, 2)
    store.begin(b.conversation, "call-b", "hb")
    store.finish(b.conversation, "call-b", {"status": "x", "execution_status": "unknown"})
    m = store.open_turn([a.token, b.token], "both")
    assert m.conversation not in (a.conversation, b.conversation)
    assert m.citable == frozenset({m.conversation, a.conversation, b.conversation})
    assert store.denials(m.conversation) == 2, "the new conversation takes the highest denial count"
    with pytest.raises(StoreError) as info:
        store.begin(m.conversation, "call-m", "hm")
    _code(info, "session_requires_reconciliation")
    assert store.denials(a.conversation) == 2 and store.begin(a.conversation, "call-a", "ha") is None, (
        "the conversations found must be left unchanged"
    )


def test_turn_of_an_unknown_or_expired_token_is_none():
    s = EvidenceStore(qa.OWNER, _limits(retention_seconds=60))
    try:
        t = s.open_turn([], "u")
        assert s.turn("nt_" + "f" * 32) is None
        real = time.time
        try:
            time.time = lambda: real() + 61
            assert s.turn(t.token) is None, "a turn must expire after retention_seconds"
        finally:
            time.time = real
    finally:
        s.close()


def test_denials_are_per_conversation(store):
    a, b = store.open_turn([], "a").conversation, store.open_turn([], "b").conversation
    store.set_denials(a, 3)
    assert (store.denials(a), store.denials(b)) == (3, 0)


# ---- journal --------------------------------------------------------------------------------------


def test_begin_of_a_new_call_returns_none(store, conv):
    assert store.begin(conv, "call-1", "h1") is None, "begin of a fresh call id must return None"


def test_begin_repeated_id_same_hash_returns_the_stored_outcome(store, conv):
    store.begin(conv, "call-1", "h1")
    rec = _record(call_id="call-1")
    store.finish(conv, "call-1", OK_OUTCOME, rec)
    got = store.begin(conv, "call-1", "h1")
    assert got == OK_OUTCOME, f"replayed begin('call-1','h1') returned {got!r}, want {OK_OUTCOME!r}"


def test_the_same_call_id_in_another_conversation_is_a_different_call(store, conv):
    store.begin(conv, "call-1", "h1")
    store.finish(conv, "call-1", OK_OUTCOME)
    other = store.open_turn([], "other").conversation
    assert store.begin(other, "call-1", "h1") is None, "another conversation must not replay this outcome"


def test_begin_repeated_id_different_hash_is_tool_call_id_conflict(store, conv):
    store.begin(conv, "call-1", "h1")
    store.finish(conv, "call-1", OK_OUTCOME)
    with pytest.raises(StoreError) as info:
        store.begin(conv, "call-1", "h2")
    _code(info, "tool_call_id_conflict")


def test_begin_of_an_unfinished_authorizing_id_is_refused(store, conv):
    store.begin(conv, "call-1", "h1")
    with pytest.raises(StoreError) as info:
        store.begin(conv, "call-1", "h1")
    e = info.value
    assert qa.error_code_matches(e, "authorization_interrupted") or qa.error_code_matches(
        e, "execution_unknown"
    ), f"begin of unfinished call-1 raised {e!r}"


def test_begin_of_an_unfinished_dispatched_id_is_refused(store, conv):
    store.begin(conv, "call-1", "h1")
    store.dispatch(conv, "call-1")
    with pytest.raises(StoreError) as info:
        store.begin(conv, "call-1", "h1")
    e = info.value
    assert qa.error_code_matches(e, "execution_unknown") or qa.error_code_matches(
        e, "authorization_interrupted"
    ), f"begin of dispatched-unfinished call-1 raised {e!r}"


def test_unknown_execution_blocks_the_conversation(store, conv):
    store.begin(conv, "call-1", "h1")
    store.dispatch(conv, "call-1")
    store.finish(conv, "call-1", {"status": "quarantined", "execution_status": "unknown"})
    with pytest.raises(StoreError) as info:
        store.begin(conv, "call-2", "h2")
    _code(info, "session_requires_reconciliation")


def test_block_holds_for_every_later_call_and_only_in_its_conversation(store, conv):
    store.begin(conv, "call-1", "h1")
    store.finish(conv, "call-1", {"status": "x", "execution_status": "unknown"})
    for n in (2, 3):
        with pytest.raises(StoreError) as info:
            store.begin(conv, f"call-{n}", f"h{n}")
        _code(info, "session_requires_reconciliation")
    other = store.open_turn([], "other").conversation
    assert store.begin(other, "call-4", "h4") is None, "a block must not reach another conversation"


def test_finish_stores_the_record_atomically_with_the_outcome(store, conv):
    store.begin(conv, "call-1", "h1")
    rec = _record(call_id="call-1")
    store.finish(conv, "call-1", OK_OUTCOME, rec)
    _assert_holds(store.get(rec["evidence_id"]), rec, "finish")


def test_max_invocations_quota():
    s = EvidenceStore(qa.OWNER, _limits(max_invocations=1, conversation_share_percent=100))
    try:
        c = s.open_turn([], "u").conversation
        s.begin(c, "call-1", "h1")
        s.finish(c, "call-1", OK_OUTCOME)
        with pytest.raises(StoreError) as info:
            s.begin(c, "call-2", "h2")
        _code(info, "storage_quota_exceeded")
    finally:
        s.close()


def test_max_records_quota():
    s = EvidenceStore(qa.OWNER, _limits(max_records=1, conversation_share_percent=100))
    try:
        c = s.open_turn([], "u").conversation
        s.begin(c, "call-1", "h1")
        s.finish(c, "call-1", OK_OUTCOME, _record(call_id="call-1"))
        with pytest.raises(StoreError) as info:
            s.begin(c, "call-2", "h2")
        _code(info, "storage_quota_exceeded")
    finally:
        s.close()


def test_a_conversation_may_use_only_its_share_of_the_store():
    s = EvidenceStore(qa.OWNER, _limits(max_invocations=4, conversation_share_percent=25))
    try:
        a, b = s.open_turn([], "a").conversation, s.open_turn([], "b").conversation
        s.begin(a, "call-1", "h1")
        s.finish(a, "call-1", OK_OUTCOME)
        with pytest.raises(StoreError) as info:
            s.begin(a, "call-2", "h2")
        _code(info, "storage_quota_exceeded")
        assert s.begin(b, "call-1", "h1") is None, "another conversation keeps its own share"
    finally:
        s.close()


def test_completed_calls_leave_the_quota_one_retention_period_after_they_began():
    s = EvidenceStore(
        qa.OWNER, _limits(max_invocations=1, conversation_share_percent=100, retention_seconds=60)
    )
    real = time.time
    try:
        c = s.open_turn([], "u").conversation
        s.begin(c, "call-1", "h1")
        s.finish(c, "call-1", OK_OUTCOME)
        time.time = lambda: real() + 61
        c = s.open_turn([], "u").conversation
        assert s.begin(c, "call-2", "h2") is None, "max_invocations must count calls per retention window"
    finally:
        time.time = real
        s.close()


# ---- records --------------------------------------------------------------------------------------


def test_put_then_get_returns_the_record(store, conv):
    rec = _record()
    store.put(rec, conv)
    _assert_holds(store.get(rec["evidence_id"]), rec, "put")


def test_get_unknown_is_evidence_not_found(store):
    with pytest.raises(StoreError) as info:
        store.get("ev_" + "0" * 32)
    _code(info, "evidence_not_found")


def test_get_of_another_conversations_record_with_citable_is_evidence_not_found(store, conv):
    rec = _record()
    store.put(rec, conv)
    other = store.open_turn([], "other").conversation
    with pytest.raises(StoreError) as info:
        store.get(rec["evidence_id"], frozenset({other}))
    _code(info, "evidence_not_found")
    _assert_holds(store.get(rec["evidence_id"], frozenset({conv})), rec, "get with its own conversation")


def test_get_expired_is_evidence_expired_and_catalog_skips_it():
    s = EvidenceStore(qa.OWNER, _limits(retention_seconds=1))
    try:
        c = s.open_turn([], "u").conversation
        rec = _record(created_at=time.time() - 30)
        s.put(rec, c)
        time.sleep(1.2)
        with pytest.raises(StoreError) as info:
            s.get(rec["evidence_id"])
        _code(info, "evidence_expired")
        ids = [r["evidence_id"] for r in s.catalog([rec["evidence_id"]], frozenset({c}), 10)]
        assert rec["evidence_id"] not in ids, f"expired {rec['evidence_id']} listed in catalog {ids!r}"
    finally:
        s.close()


def test_get_of_a_tampered_record_is_evidence_integrity_failure(store):
    rec = _record(marker="QA_MARKER_ORIGINAL")
    store.put(rec)
    changed = qa.tamper_database(store.db, "QA_MARKER_ORIGINAL", "QA_MARKER_TAMPERED")
    assert changed, "tamper helper found no stored copy of the content marker"
    with pytest.raises(StoreError) as info:
        store.get(rec["evidence_id"])
    _code(info, "evidence_integrity_failure")


def test_catalog_of_an_empty_store_is_empty(store, conv):
    assert store.catalog(["ev_" + "0" * 32], frozenset({conv}), 20) == [], (
        "catalog of a fresh store must be []"
    )


def test_catalog_is_newest_first_and_respects_limit(store, conv):
    t = time.time()
    recs = [_record(created_at=t - 30 + i) for i in range(3)]
    for r in recs:
        store.put(r, conv)
    visible = [r["evidence_id"] for r in recs]
    ids = [r["evidence_id"] for r in store.catalog(visible, frozenset({conv}), 10)]
    want = [r["evidence_id"] for r in reversed(recs)]
    assert ids == want, f"catalog order {ids!r}, want newest first {want!r}"
    assert len(store.catalog(visible, frozenset({conv}), 2)) == 2, "catalog limit 2 must return 2 entries"


def test_catalog_lists_only_visible_ids_of_citable_conversations(store, conv):
    other = store.open_turn([], "other").conversation
    mine, unseen, theirs = _record(), _record(), _record()
    store.put(mine, conv)
    store.put(unseen, conv)
    store.put(theirs, other)
    visible = [mine["evidence_id"], theirs["evidence_id"]]
    ids = [r["evidence_id"] for r in store.catalog(visible, frozenset({conv}), 10)]
    assert ids == [mine["evidence_id"]], (
        f"catalog {ids!r} must hold only the visible record of its conversation"
    )


@pytest.mark.parametrize("excluded", ["plugin_refusal", "execution_unknown", "invalid"])
def test_catalog_excludes_refusals_unknown_outcomes_and_invalid_evidence(store, conv, excluded):
    keep = _record()
    drop = _invalid_executor_record() if excluded == "invalid" else _record(kind=excluded)
    store.put(keep, conv)
    store.put(drop, conv)
    ids = [
        r["evidence_id"]
        for r in store.catalog([keep["evidence_id"], drop["evidence_id"]], frozenset({conv}), 10)
    ]
    assert keep["evidence_id"] in ids, f"local_result missing from catalog {ids!r}"
    assert drop["evidence_id"] not in ids, f"{excluded} record listed in catalog {ids!r}"


# ---- writ claims ----------------------------------------------------------------------------------


def test_claim_writ_true_once_then_false(store):
    assert store.claim_writ("w-1", "n-1") is True, "first claim_writ('w-1','n-1') must be True"
    assert store.claim_writ("w-1", "n-1") is False, "second claim_writ('w-1','n-1') must be False"


def test_claim_writ_is_per_nonce(store):
    store.claim_writ("w-1", "n-1")
    assert store.claim_writ("w-2", "n-2") is True, "a different nonce must claim"


def test_claim_writ_lasts_as_long_as_the_store(store):
    assert store.claim_writ("w-1", "n-1") is True
    store.put(_record())
    assert store.claim_writ("w-1", "n-1") is False, "claim of n-1 forgotten"


# ---- citations ------------------------------------------------------------------------------------


def test_mark_citations_never_downgrades_accepted(store):
    # The spec gives no read path for citation status; this observes it through get().
    rec = _record()
    store.put(rec)
    store.mark_citations([rec["evidence_id"]], "accepted")
    store.mark_citations([rec["evidence_id"]], "submitted_unknown")
    got = json.dumps(store.get(rec["evidence_id"]))
    assert "accepted" in got and "submitted_unknown" not in got, (
        f"after accepted then submitted_unknown, record shows {got[:400]!r}"
    )
