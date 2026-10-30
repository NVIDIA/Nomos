<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# 7. The evidence store and recovery

[← Architecture index](README.md) · previous: [Writ handling and executors](06-writ-handling-and-executors.md)

The plugin's memory and audit trail: **one SQLite `:memory:` database in the worker**
([`repository.py`](../../nemo_relay_nomos/repository.py) `EvidenceStore`). No file, no service;
it lives as long as the worker (Relay never respawns one; a new activation starts empty). Everything but the nonce
ledger and the (opt-in) tool lists belongs to one conversation. Every access is **C6**.

## Where it lives, and who touches what

![How long the evidence lasts: the store is SQLite :memory: inside the worker, with no file and no service; the worker starts with an empty store, serves every conversation, and when it exits the whole store is gone; a new activation (only if an operator restarts it, since Relay never respawns a worker) reads old ids as evidence_not_found. Inside it, with R = limits.retention_seconds: a record's content is citable for R, then a tombstone (evidence_expired) until 2R, then the row is gone; a turn lives R, then unknown_turn; a completed call replays its outcome in its own conversation for R, then the same id is a new call; a writ claim is kept for the worker's whole life](../figures/store.png)

Created when the plugin registers; two activations never share records.

{{code nemo_relay_nomos/repository.py EvidenceStore.__init__}}

| Table | Written by | Read by |
|---|---|---|
| `conversations` | H1: `open_turn` (a new conversation); H3: `set_denials`; `finish`: `blocked` | H3: `begin` (blocked), `denials` (denial cap) |
| `turns` | H1: `open_turn`, one per model request | H1: `open_turn` (which conversation the messages show); H3: `turn` (the call's conversation and user request) |
| `tool_lists` | H1: `open_turn`, only with `tool_definitions` on; one row per distinct tool list | H3: `tool_list` (the [observed tool definitions](../../DESIGN.md#observed-tool-definitions) of the call's turn) |
| `records` | H3: refusals and denials (`put`, `finish`); writ handler: results (`finish`); citations update `citation` (`mark_citations`) | H1: the catalog (`catalog`); H3: cited records (`get`) |
| `calls` | H3: `begin` (T1, `authorizing`); writ handler: `dispatch` (T2, `dispatching`); `finish` (T3, `complete` + outcome) | H3: `begin` (replay, conflict, blocked checks) |
| `writ_claims` | writ handler: `claim_writ` (T2), before any dispatch | `claim_writ` (a nonce already present = `writ_replayed`) |

## The tables, with the rows from a real run

```mermaid
erDiagram
  conversations {
    TEXT id PK "cv_ + 32 hex (never shown to the model)"
    REAL created
    INTEGER denials "consecutive Council denials"
    INTEGER blocked "1 after an unknown outcome"
  }
  turns {
    TEXT token PK "nt_ + 32 hex (the model copies it into nomos.turn)"
    TEXT conversation FK
    TEXT citable "conversations whose evidence it may cite (JSON)"
    TEXT user_request "latest user text, at most 800 chars"
    REAL created
    REAL expires "created + retention"
    TEXT tools_sha256 FK "NULL unless tool_definitions is on"
    INTEGER tools_kept "1 when the list is admitted for this conversation"
    INTEGER superseded "1 once a later request's messages showed it"
  }
  tool_lists {
    TEXT sha256 PK "of the canonical tools array"
    BLOB tools "the tools as the application offered them"
  }
  records {
    INTEGER seq PK "catalog order"
    TEXT id UK "ev_ + 32 hex"
    TEXT conversation FK "NULL for a refusal that named no live turn"
    TEXT kind "executor_result · local_result · council_denial · …"
    TEXT status "locally_verified · plugin_signed · plugin_certified · invalid"
    REAL created
    REAL expires "created + retention"
    BLOB record "the whole record (canonical JSON); erased at expiry"
    TEXT digest "sha256 of record"
    TEXT citation "not_submitted · submitted_unknown · accepted"
  }
  calls {
    TEXT conversation PK
    TEXT id PK "tool_call_id"
    TEXT request_hash "sha256(tool + args)"
    TEXT state "authorizing · dispatching · complete"
    BLOB outcome "the response returned (replayed on repeat)"
    TEXT digest
    REAL created
  }
  writ_claims {
    TEXT nonce PK
    TEXT writ_id
    REAL claimed
  }
  conversations ||--o{ turns : "named by (none once its turns expire, while a call is unfinished)"
  tool_lists |o--o{ turns : "offered in"
  conversations ||--o{ calls : "journal"
  conversations ||--o{ records : "evidence"
  calls ||--o| records : "outcome names evidence_id"
```

The rows after the five 🧪 demonstration calls (times in UTC), dumped after every call by a capture-only worker
entry in the integration test bed, since the worker's memory is unreachable from outside.

### `conversations` and `turns`: who is calling

{{table store_snapshot.json conversations cols=id,created,denials,blocked trim=24}}

{{table store_snapshot.json turns cols=token,conversation,citable,user_request,created,expires trim=22}}

One conversation, five turns: from the second request on, H1 found the earlier tokens in the messages and continued it
([how](02-request-path-h1.md#e2--the-turn-token-and-the-conversation--c3-c6)). `user_request` is the Council's context
for calls in reply to that request; `denials` and `blocked` never cross conversations.

### `records`: one row per response

{{table store_snapshot.json records cols=seq,id,conversation,kind,status,citation,bytes,created,expires trim=18}}

| Column | |
|---|---|
| `seq` | catalog order (newest first) |
| `conversation` | the owner; a citation from any other reads `evidence_not_found` (row 5, a missing intent, has none) |
| `record`, `digest` | the whole record as canonical JSON (not shown) and its SHA-256, re-checked on every read ([anatomy](05-evidence-and-citation.md#anatomy-of-a-record)) |
| `expires` | `created` + `retention_seconds` (1 h): content erased, id kept as a tombstone (`evidence_expired`) |
| `citation` | `not_submitted` → `submitted_unknown` (in flight or failed) → `accepted` (valid writ; never downgraded). Rows 1 and 3 were cited and accepted |

Refusals are a ring of the latest 200 per conversation, so they can never exhaust the quota.

{{code nemo_relay_nomos/repository.py EvidenceStore.put}}

### `calls`: the journal, one row per guarded tool call

{{table store_snapshot.json calls cols=conversation,id,request_hash,state,outcome_bytes,created trim=22}}

Keyed by (conversation, `tool_call_id`), so numbered ids (`call_0`) never replay across conversations.
`request_hash` (tool + arguments) detects `tool_call_id_conflict`; `outcome` is the exact response, replayed for a
repeat. Four rows for five calls: the missing-intent call was refused before the journal.

### `writ_claims`: the plugin's nonce ledger

{{table store_snapshot.json writ_claims cols=nonce,writ_id,claimed trim=28}}

Three approvals, three claims; every writ is claimed here before dispatch (and by the Executor for `nomos_http`).

{{code nemo_relay_nomos/repository.py EvidenceStore.claim_writ}}

## Reading: the catalog and a citation

The catalog takes the newest valid records among the ids the messages show and the turn's citable conversations;
each passes `get` (expiry, row digest) and the receipt check. A citation reads through the same `get`.

{{code nemo_relay_nomos/repository.py EvidenceStore.catalog}}

{{code nemo_relay_nomos/repository.py EvidenceStore.get}}

## A record's life

![One evidence record, from build to expiry, only with a signing key: 1 build and seal (plugin-signed or Executor-signed, plus a plugin receipt on all); 2 persist, before the model sees it (at T3, or alone if refused early); 3 offer in the catalog (results, denials and errors only: id, tool and arguments, never output); 4 the model cites the id in a later call's nomos; 5 re-check (receipt re-checked, certified re-verified on the current keys); 6 mark submitted_unknown, then send to the Council; 7 settle (a valid writ marks it accepted; never downgraded); 8 expire (content erased, then the id). Refusals, unknown outcomes and quarantined results are stored but never offered](../figures/record-lifecycle.png)

1–3 inside the producing call, before the model sees anything; 4–7 on later turns
([with data](05-evidence-and-citation.md)); 8 after retention. A refusal before T1 (`invalid_arguments`,
`missing_intent`, `unknown_turn`, tool-policy and trust refusals) is stored by `put` with no journal row. Only
`executor_result`, `local_result`, `council_denial` and `council_error` records that are not quarantined are ever
offered in a catalog. A `plugin_refusal` or `execution_unknown` record is never offered, but its id (which the
tool result showed) can still be cited from a turn that may cite its conversation; a quarantined record cannot.

## The journal: authorizing, dispatching, complete

T1, T2 (two writes: the nonce claim, then `dispatching`) and T3, the network only between them; marking cited
records and counting denials are separate writes of their own. While the worker lives, any failure leaves a state
the next call can act on: a lost answer after dispatch blocks the conversation rather than risk running twice. If the
worker dies, the call fails closed and the next activation starts empty.

![When the call journal is written, time running left to right: T1 (step 13) checks, then writes the row authorizing; the Council is asked (16) and answers (17); a denial or Council error skips T2 from the decision (17), straight to T3; the writ is verified (18), and a refused writ also skips T2; T2 (19) claims the nonce, then moves the row to dispatching; the Executor is called (20) and answers (21), or for a local tool next_ runs through Relay; T3 (23) writes record and row complete at once; then the result is returned (24). Shaded: window 1, after T1 and before T2, and window 2, after T2 and before T3; what a failure in each leaves behind is in the table under the figure](../figures/journal.png)

| A failure in… | What it leaves behind |
|---|---|
| window 1 (after T1, before T2): the Council errs, times out or is unreachable | ✕ `council_error`, journaled at T3 (with a key, a 🔏 record); nothing ran; the denial count is unchanged |
| window 1: the call is cut off before T3 | ⊘ the row stays `authorizing`; the same id again reads `authorization_interrupted`; other calls proceed |
| window 2 (after T2, before T3): no, oversized or unreadable Executor answer | ? `execution_unknown`, journaled at T3; the conversation is blocked; the action may have run, so nothing retries it |
| window 2: the call is cut off before T3 | ? the row stays `dispatching`; the conversation is blocked until an operator restarts the worker, or until its transcript shows no live turn token |

**T1** before C4: `begin` ([shown in H3, E3](03-governed-tool-call-h3.md#e3--the-journal-entry--c6-transaction-t1)).
**T2** before C5: claim, then `dispatching`:

{{code nemo_relay_nomos/repository.py EvidenceStore.dispatch}}

**T3** before the model sees anything: record + outcome together (an unknown outcome also sets `blocked`):

{{code nemo_relay_nomos/repository.py EvidenceStore.finish}}

| What happens next | Result |
|---|---|
| Same `tool_call_id`, same arguments, journal `complete` | the stored outcome, replayed; nothing is re-sent |
| Same id, different arguments | `tool_call_id_conflict` |
| Same id, journal `authorizing` (an earlier attempt ended before deciding) | `authorization_interrupted` for that id; other calls proceed |
| Same id, journal `dispatching` (the answer was lost after dispatch) | `execution_unknown` for that id |
| Any new call id that reaches T1 in a conversation with a `dispatching` entry, or after an `unknown` outcome or a quarantine in it | `session_requires_reconciliation` (checks before T1 still answer first; other conversations proceed) |
| SQLite failure (for example, out of memory) | `storage_unavailable`, `execution_status: unknown`; no detail is exposed |
| The worker dies | the store is gone; the call fails closed; the next activation starts empty, so old evidence ids read `evidence_not_found` |

| Why | |
|---|---|
| strict | the pinned Nomos cannot report a writ's fate after a lost response, and a new writ could run the action twice; reconciling is an operator action (today: restart the worker); a transcript trimmed of all its live turns escapes the block |
| a dead worker | its evidence is lost and re-created through the Council; a call retried after dying mid-dispatch gets a new writ (the Executor's nonce stops only the same one). Accepted ([limits](10-limits-and-open-items.md)) |

## Retention

A worker may live for weeks; nothing grows without bound:

| Row | Removed |
|---|---|
| a record's content | at its expiry (`created + retention_seconds`); the id stays as a tombstone |
| a record's tombstone | one more retention period later |
| a completed journal entry | one retention period after the call began |
| a turn | at its expiry; also the oldest first beyond `max_invocations` turns |
| a conversation | when no live turn names it and it has no unfinished call |
| a writ claim | never, while the worker lives |

{{code nemo_relay_nomos/repository.py EvidenceStore._purge}}

## Limits

Store-wide, and per conversation at most `conversation_share_percent` of records, journal entries and storage:

| Limit | Default | Enforced |
|---|---:|---|
| `max_result_bytes` | 2 MiB | Executor response read (streamed, bounded), result size, tool arguments |
| `max_storage_bytes` | 64 MiB | memory: logical bytes of records + journal; capacity for one more result per call in flight is reserved before dispatch |
| `max_records` | 1,000 | live records |
| `max_invocations` | 10,000 | journal entries within one retention period; also the most live turns |
| `retention_seconds` | 1 hour | record content erased at expiry; the id tombstone stays one more period |
| `conversation_share_percent` | 25 | each conversation's share of records, journal entries and storage |
| `max_citation_bytes` | 4 MiB | total size of certified originals in one request |
| `max_output_chars` | 65,536 | stdout / stderr / local output in the model-visible result (the stored record is whole, except a `local` result whose encoding exceeds `max_result_bytes`: it is stored as a string bounded to this) |

Next: [components](08-components.md).
