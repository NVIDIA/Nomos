<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: LicenseRef-NvidiaProprietary

NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
property and proprietary rights in and to this material, related
documentation and any modifications thereto. Any use, reproduction,
disclosure or distribution of this material and related documentation
without an express license agreement from NVIDIA CORPORATION or
its affiliates is strictly prohibited.
-->

# Tier-1b — 1-to-1 ledger writes: implementation plan + log

Spec: `Nomos-Astra-Deploy/docs/nomos-1-to-1-ledger-write-design.md`. Branch:
`pg-ha/nomos-5b-workflow-store` (continues after NOMOS-5b). Goal: a normal review
drops from ~7 serialized ledger appends to **1** consolidated `INTENT_REVIEW_RECORDED`.

## Design decision — append suppression is FLAG-GATED (legacy preserved)

The five review methods append today AND are used by both the deployed
propose/authorize path and legacy/in-process callers + many tests. So suppression
is gated, not unconditional:

- `CouncilConfig.consolidated_review_audit: bool = False` (default → byte-identical
  legacy behavior; every existing test stays green untouched).
- `QuorumManager(record_audit_entries: bool = True)` — council passes
  `not consolidated_review_audit` down to its internally-built quorum manager.
- Deployed `CouncilService` (functions.py:597) constructs the council with
  `consolidated_review_audit=True`.

When the flag is ON, these appends are SKIPPED:
- council.submit_intent → INTENT_SUBMITTED, INTENT_EVALUATED
- quorum.open_vote → QUORUM_VOTE(opened)
- quorum.cast_vote → QUORUM_VOTE(voted)
- quorum.revise_vote → DEBATE_VOTE
- quorum._close_vote → DECISION_MADE

Token-mint / execution / verification appends are PRESERVED (design: only the
*review* entries collapse).

## Consolidated append — placed at the orchestration layer (functions.py)

One `INTENT_REVIEW_RECORDED` per ACCEPTED intent that reaches a final outcome.
Exit-point map (0 vs 1 entry):

propose_intent:
- auth/validation/council-not-found/rate-limit early returns (≈1522/1535/1549/1560/1584/1609) → **0** (intent not accepted)
- OPA deny (1639) → **1** (final.state=denied, opa block, no votes)
- submit failed (1710): policy DENY (workflow.evaluation_result.decision==DENY) → **1**; else (justification/max-pending) → **0**
- post-review (1738) → **1** (approved/denied/eval-error)

authorize: same shape — OPA deny (2148→) **1**; submit-failed policy DENY **1** else **0**; post-review denied **1**; approved → **1 appended BEFORE writ mint**, seq recorded in workflow.ledger_sequences.

Helper: `_record_consolidated_review(intent, *, workflow, opa, votes, chairman, code_review, final_state, request_type)` builds the schema-v1 payload (request/opa/policy/chairman/code_review/quorum+votes/debate/final/timing) — NO tokens/secrets/keys/raw stdout — appends once (idempotent), records seq. No-op unless `consolidated_review_audit`.

## Idempotency (as built — honest semantics)
- **PG mode (deployed):** migration `0004_intent_review_idem.sql` adds a partial
  unique index on `(chain_id, (payload_json->>'intent_id'))` WHERE
  `entry_type='intent_review_recorded'`. `_record_consolidated_review` wraps the
  append and, on a unique violation (SQLSTATE 23505, detected via
  `_is_unique_violation` walking the exception chain), treats the duplicate as
  **already-recorded → no-op** (does not 500, does not append a second row, chain
  stays intact). It does NOT read the existing entry back — "already recorded" is
  sufficient for the audit guarantee.
- **File/dev mode:** no DB guard and no scan — relies on intent_id being a random
  uuid (dupes unreachable in normal use). A *forced* replay of the same intent_id
  in file mode would append twice; acceptable for dev. Not implementing the
  spec's "best-effort scan" (O(n) per append, dev-only path).
- Each request calls `_record_consolidated_review` exactly once (the propose/
  authorize exit points are mutually exclusive), so within-request double-writes
  cannot occur; the index guards only cross-request replays.

## Risk / note
- External ledger readers that count DECISION_MADE per review will see the new
  `intent_review_recorded` type instead. Historical entries stay readable. Monitor
  reader update = separate follow-up if any reader depends on the old type.

## Steps (check as done)
- [x] entry.py: add INTENT_REVIEW_RECORDED
- [x] CouncilConfig.consolidated_review_audit flag
- [x] QuorumManager.record_audit_entries flag + gate 4 append sites
- [x] council.submit_intent: gate 2 append sites
- [x] payload builder (review_audit.py) + _record_consolidated_review helper
- [x] wire consolidated append at propose/authorize exit points (OPA-deny, submit-policy-DENY, post-review deny, approved-before-mint)
- [x] functions.py: construct council with consolidated_review_audit=True
- [x] migration 0004 idempotency index (applied to test PG, verified)
- [x] tests (tests/test_one_to_one_ledger.py, 8): legacy flag-off writes intermediates + no consolidated; flag-on suppresses all 5 review types; token-mint preserved; payload builder x2; orchestration propose==1, authorize==1, 16-concurrent==16
- [x] ruff clean + full suite 89 passed / 10 skipped
- [ ] codex adversarial sign-off on 1b tests (operator gate)

## Status 2026-06-15
IMPLEMENTED on branch pg-ha/nomos-5b-workflow-store (committed c12f2d7; follow-up
fixes for codex review in progress). Open follow-up: external ledger readers/monitor
that count DECISION_MADE per review must learn the new intent_review_recorded type
(separate task if any reader depends on it).

### codex review round 1 (c12f2d7) — fixes applied
- **authorize dropped debate_history** (consolidated record lacked debate detail):
  authorize now captures debate_history from `_collect_review_outcome` and passes it
  to both the denied and approved `_record_consolidated_review` calls.
- **idempotency docs vs impl contradiction:** impl now catches the unique violation
  and no-ops (see Idempotency above); both docs updated to honest semantics.
- **test gaps:** added authorize-debate payload, duplicate no-op, and an honest
  redaction-boundary test (intent passthrough is documented, not a redaction claim).
