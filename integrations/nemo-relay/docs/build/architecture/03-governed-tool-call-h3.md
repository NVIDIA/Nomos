<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# 3. A governed tool call (H3), end to end

[← Architecture index](README.md) · previous: [The model request (H1)](02-request-path-h1.md)

One approved command, from the model's tool call to the result it reads: each step names its connection
([map](README.md#the-picture-five-places-eight-connections)), shows the real data (🧪 unless 🌐) and then the code.
`run_shell` is a `nomos_http` tool: the Executor runs it, never the agent's own function.

Two figures, numbered on from [H1's](02-request-path-h1.md) (1–9). First, what happens before the call may run;
each mark on the H3 line ends the call there, and every ending still returns to the application as a tool result:

![Steps 10 to 17, time running down: tools.execute (C1) reaches H3 (C3); refused: invalid_arguments or missing_intent, the result showing the field to send; the turn is looked up (C6), refused if unknown or expired: unknown_turn, the result saying to call again with the latest turn; a second concurrent call of the conversation: session_busy; tool policy, trust snapshot or Council key fails. A line: none of these writes a journal row. 13 T1 writes the journal row authorizing (C6); the same tool_call_id and arguments again in this conversation return its stored outcome; a conflict, blocked conversation or full quota is refused by the store with no journal row and nothing stored. A line: every ending below is journaled at T3 (step 23) first. 14 the cited records, only from conversations this turn may cite, and the denial count (C6): a failed citation, a turn a later request has already shown (with tool_definitions on: unknown_turn) or the denial cap refuses without asking the Council; 15 the cited records are marked submitted_unknown (C6); 16 council_authorize (C4); 17 the decision, with a signed writ if approved; a denial (count + 1) or council_error (count kept): nothing ran, journaled at T3, and the model's tool result (C3) is the Council's whole answer but the writ: reasons, votes, missing permissions](../figures/h3-authorize.png)

Then, once the Council approves:

![Steps 18 to 26, time running down: 18 the writ is verified (the Council's key, this exact call, fresh), a failure refused and journaled at T3 with the denial count not reset; 19 T2 claims the nonce, then marks the row dispatching (C6), an already-claimed nonce is writ_replayed for any tool; then one of two branches: nomos_http, 20 POST /execute (C5) and 21 the Executor-signed ExecutionResult, its signature verified: executed or execution_failed; or execution_unknown (no, oversized or unreadable answer) or evidence_invalid (fails verification): the call may have run and is never retried; local, 20 next_ with the arguments minus nomos through Relay (C3) to the app's tool and 21 its output, executed or execution_failed (the tool raised), plugin-certified when there is a signing_key; 22 the writ was valid, so cited records become accepted and the denial count resets to 0 (C6); 23 T3 completes the row together with the record, and after an unknown outcome blocks the conversation (C6); 24 the tool result goes to Relay (C3) with a note only the host sees (the Council's votes, timings, trace); 25 the application gets the tool result (C1); 26 the model reads it on its next request](../figures/h3-dispatch.png)

A record exists only with a `signing_key`; without one, T3 completes the journal row alone. `execution_failed` on the
`nomos_http` branch can also mean the Executor refused to run the writ. The host-only note also carries the Council's
votes; on a denial the model sees them too, in the Council's answer.

The sections below follow the figures' step numbers:

| Section | Steps in the figure |
|---|---|
| E1 · the call arrives | 10–11 |
| E2 · guard, split, validate, find the conversation | the checks after 11, and 12 |
| E3 · the journal entry (T1) | 13 |
| E4 · citations | 14–15 |
| E5 · `council_authorize` | 16 |
| E6 · the decision | 17 |
| E7 · the writ, decoded and verified | 18 |
| E8 · claim, then dispatch (T2) | 19, and the local branch of 20–21 |
| E9 · the Executor runs it (`nomos_http`) | 20 (`nomos_http`) |
| E10 · strict verification | 21 |
| E11 · the record is stored (T3) | 22–23 |
| E12 · what the model and the host receive | 24–25 |

What the model sees and what only the host sees (E12):

| | The model (the tool result, over C3 then C1) | Only the host (the `{"nomos": …}` annotation) |
|---|---|---|
| any guarded call | `status`, `execution_status`, `evidence_status`, `evidence_id` (with a key), `writ_id`, the output or `message` | the same status fields, the conversation id, timings |
| an approval | the result: `stdout`/`stderr`/`exit_code` or the local tool's output, `decision_id` | what was submitted (command or operation, the certified citations), the Council's status, `decision_id` and votes |
| a denial or Council error | the Council's whole response under `council` (bounded, writ removed: reason, guidance, missing permissions, votes, retryable) and a readable `message` | the same Council status and votes |

## E1 · The call arrives · C1, C3

The agent calls `tools.execute`; Relay opens a tool scope, and subscribers (the dashboard) see the start event:

{{example tool_events.json 0}}

## E2 · Guard, split, validate, find the conversation

An unguarded tool passes straight through (`next(args)`, nothing stored). A guarded one:

| Step | ⊘ Refused as (the Council is never called) |
|---|---|
| split `nomos` from the tool's arguments; the tool never sees it | — |
| validate the intent: turn `nt_`+32 hex, four non-blank strings ≤ 4096 chars, ≤ 20 unique `ev_` ids, no other keys (no `evidence` without a signing key) | `missing_intent`, with teaching text |
| look the turn up: everything after runs in [its conversation](02-request-path-h1.md#e2--the-turn-token-and-the-conversation--c3-c6) | `unknown_turn` |
| one guarded call at a time per conversation (others proceed) | `session_busy` |
| a local tool has an operator-configured opaque subject and JSON arguments within the 32,000-byte and 640-decimal-digit integer bounds | `missing_tool_subject`, `tool_arguments_too_large` or `tool_integer_too_large` |
| a `nomos_http` tool carries exactly its `command_argument` | `invalid_command_arguments` |
| a stable `tool_call_id` | `missing_tool_call_id` |

Every refusal is listed in [every outcome](04-outcomes.md).

{{code nemo_relay_nomos/plugin.py NomosPlugin._guarded start="arguments = json.loads" end="self._busy.discard(turn.conversation)"}}

{{code nemo_relay_nomos/plugin.py NomosPlugin._in_conversation start="call_id = ctx.tool_call_id" end="command = arguments.get(policy.command_argument)"}}

## E3 · The journal entry · C6 (transaction T1)

Before any network call: journal the call `authorizing` under (conversation, `tool_call_id`) with a hash of tool and
arguments, and reserve quota for one result (store-wide and the conversation's share), in one transaction.

| Seen before in this conversation | Result |
|---|---|
| same id, same arguments | the stored outcome, replayed; nothing is sent twice |
| same id, different arguments | `tool_call_id_conflict` |
| an earlier unknown outcome | `session_requires_reconciliation` (other conversations unaffected) |

The row at the end of the run (at this moment it is still `authorizing`):

{{table store_snapshot.json calls rows=0 cols=conversation,id,request_hash,state,outcome_bytes,created trim=24}}

{{code nemo_relay_nomos/repository.py EvidenceStore.begin}}

## E4 · Citations · C6

Each cited id is loaded with the turn's citable conversations (any other reads as `evidence_not_found`) and
integrity-checked. ✅ Executor results are re-verified against the *current* trust snapshot, and the plugin's certified records
(local results, Council decisions) against its own key; each goes whole as `certified_evidence`. Nothing goes as
asserted `evidence`, and a refusal or unknown outcome cannot be cited (`evidence_not_citable`). This call cites nothing; the cited case:
[evidence and citation](05-evidence-and-citation.md#a--certified-citation-the-full-original-goes-to-the-council).

## E5 · `council_authorize` · C4

Exactly what the Council received (token redacted):

{{example council_requests.json 0}}

| Kind | Fields |
|---|---|
| **observed** | all calls: `agent_id`, `council_id`, bounded latest-user `context` from the call's own turn (marked when truncated); local: structured `tool_call` with exact business arguments and configured subject; `nomos_http`: `command`, `working_directory` |
| **asserted** | `description`, `justification`, `target_resource`, `blast_radius` |
| **cited** | `certified_evidence` (signed originals); `evidence` is always empty |
| **observed, certified** | with [`tool_definitions`](../../DESIGN.md#observed-tool-definitions) on: the last `certified_evidence` item, the tool definitions H1 saw, signed with the plugin's registered key; `context` names its digest |
| **cited, certified by the plugin** | a cited [local result](../../DESIGN.md#certified-local-results), as the `ExecutionResult` the plugin signed when the tool returned under its verified writ, or a cited [Council decision](../../DESIGN.md#certified-council-decisions), as the one it signed when the answer arrived |

{{code nemo_relay_nomos/plugin.py NomosPlugin._in_conversation start="intent = args[\"nomos\"]" end="decision = await self._council"}}

## E6 · The decision · C4

The approval (the writ is the Council's base64 blob, shortened):

{{example council_decisions.json 0 trim=72}}

🌐 live, the plugin's host-only annotation for the same kind of call (votes, timings):

{{example live_run.json annotations.0}}

## E7 · The writ, decoded and verified

{{example writ_example.json decoded trim=72}}

`DefaultWritHandler` verifies it before anything is dispatched:

| Check | Fails as |
|---|---|
| Wire schema, including `max_executions == 1` and no packaged files | `invalid_writ_contract` |
| Ed25519 signature by the **pinned** Council key (or, opt-in, the key fetched once from the Council) over the Nomos canonical writ fields | `invalid_signature` |
| `target_agent_id`, `council_id`, `command`, `working_directory` and `writ_id` match this call exactly | `writ_binding_mismatch` |
| `issued_at < expires_at`, not issued in the future, not expired at dispatch | `invalid_writ_time`, `writ_expired` |
| Issued for **this** request: not more than 30 s before the Council was asked | `writ_not_fresh` |
| Trust snapshot still valid | `trust_policy_expired` |
| With `council_public_key_source = "council"`: the Council's key obtained (before the Council is asked) | `council_key_unavailable` |

Pinned by `test_writ_for_another_command_is_refused_and_never_dispatched`.

{{code nemo_relay_nomos/verify.py verify_writ}}

## E8 · Claim, then dispatch · C6, C5

A writ is used once: the plugin records its nonce first (always), and for `nomos_http` the Executor claims it too
(`SET NX` in Redis; live, a second use is refused, [finding](../findings.md)). The journal moves to `dispatching`:
from here a lost response is **unknown** and blocks the conversation rather than re-sending. The ledger row (T2):

{{table store_snapshot.json writ_claims rows=0 cols=nonce,writ_id,claimed trim=28}}

The handler: validate, claim, `dispatching`, dispatch.

{{code nemo_relay_nomos/writs.py DefaultWritHandler.handle}}

## E9 · The Executor runs it · C5

The Executor re-checks the writ, claims its nonce, runs exactly the approved command, and signs the outcome (a
whitelist refusal is signed too):

{{example executor_results.json 0.result trim=72}}

{{code nemo_relay_nomos/writs.py DefaultWritHandler._nomos_http start="result = await self.executor.execute" end="error = str(exc)"}}

## E10 · Strict verification

`verify_executor_evidence` accepts nothing it cannot check; any mismatch quarantines the result (output withheld,
`execution_status: unknown`, conversation blocked):

| What is checked | Against |
|---|---|
| Signer key | in `trust.executors[executor_id]`. An embedded key is never trusted by itself. |
| `key_fingerprint` (twice) | SHA-256 of that key |
| Signature | Ed25519 over the canonical evidence payload |
| `writ_id`, `intent_hash`, `council_id`, `target_agent_id`, `nonce`, `writ_signature` | the verified writ |
| `command`, `working_directory` | the verified writ |
| `success`, `exit_code`, `error`, `verification_stage`, `duration_seconds` | the signed `result` |
| stdout / stderr | their signed SHA-256 **and** byte counts |
| `signed_at` | within the writ's lifetime, not in the future |

{{code nemo_relay_nomos/verify.py verify_executor_evidence}}

## E11 · The record is stored, before anyone sees it · C6 (transaction T3)

One transaction writes the record (the original `ExecutionResult` with signer, timestamps, content hash and plugin
receipt; [anatomy](05-evidence-and-citation.md)) and the journal's final outcome. Without a signing key only the
outcome is written.

{{example evidence_records.json 0.record keys=evidence_id,kind,certified,evidence_status,signer,created_at,content_sha256,receipt,writ_id trim=72}}

The `records` row and the journal row moving to `complete`:

{{code nemo_relay_nomos/repository.py EvidenceStore.finish}}

## E12 · What the model and the host receive · C3, C1

The model receives the tool result: the output (bounded by `limits.max_output_chars`), the `evidence_id` it can
cite later, and who signed it:

{{example tool_results.json 0.result trim=72}}

The host receives the same result plus a **host-only annotation**. The model never sees it. It holds the decision,
the votes, what was submitted, and the timings (ms). Relay's trace exports and subscribers get a sanitized copy:
the status fields, the Council's status and decision id, and the timings, never the submitted command, the votes or
the conversation id:

{{example tool_events.json 1.annotation}}

The annotation is split off the result here, so it reaches Relay but never the model:

{{code nemo_relay_nomos/plugin.py NomosPlugin._out}}

The whole guarded path is `NomosPlugin._guarded` and `NomosPlugin._in_conversation`, shown in [Components](08-components.md#the-two-hooks).

Next: [every other way a call can end](04-outcomes.md).
