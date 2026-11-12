<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Findings: Relay and Nomos behaviour the plugin depends on

What building and testing the plugin surfaced that the Relay and Nomos documentation does not cover. Each entry
names the test that pins it. Entries pinned by the live, agent-driven tier are pinned in the integration test bed.

| Area | Finding | Date |
|---|---|---|
| Relay | [R13 (new): Relay's codecs cannot patch back more than one edited tool per request](#r13-new-relays-codecs-cannot-patch-back-more-than-one-edited-tool-per-request) | — |
| Nomos | [Plugin-signed evidence is not yet certifiable to Nomos](#plugin-signed-evidence-is-not-yet-certifiable-to-nomos) | — |
| Nomos | [Certified evidence needs the Executor's key in the durable trust store](#certified-evidence-needs-the-executors-key-in-the-durable-trust-store) | — |
| Nomos | [The Executor has its own tool whitelist: approved does not mean executed](#the-executor-has-its-own-tool-whitelist-approved-does-not-mean-executed) | — |
| Nomos | [Standalone worker baseline: credentials, evidence and harness boundaries](#standalone-worker-baseline-credentials-evidence-and-harness-boundaries-2026-10-06) | 2026-10-06 |
| Relay | [Relay 0.10.0: tool execution intercepts have no 30 s worker RPC deadline](#relay-0100-tool-execution-intercepts-have-no-30-s-worker-rpc-deadline-2026-10-12) | 2026-10-12 |
| Relay | [Relay strips a managed worker's environment, so `{"env"}` references cannot reach it](#relay-strips-a-managed-workers-environment-so-env-references-cannot-reach-it-2026-10-12) | 2026-10-12 |
| Relay | [Relay rejects `writeOnly` under `anyOf` in a plugin config schema](#relay-rejects-writeonly-under-anyof-in-a-plugin-config-schema-2026-10-12) | 2026-10-12 |
| Relay | [`nemo-relay plugins add` pip-installs the bundle and ignores `uv.lock`](#nemo-relay-plugins-add-pip-installs-the-bundle-and-ignores-uvlock-2026-10-12) | 2026-10-12 |
| Nomos | [The live Council denied the former rendered non-command tool at its OPA stage](#the-live-council-denied-the-former-rendered-non-command-tool-at-its-opa-stage-2026-10-12) | 2026-10-12 |
| Nomos | [The live Council reasons over certified evidence](#the-live-council-reasons-over-certified-evidence-2026-10-12) | 2026-10-12 |
| Relay | [A worker lives as long as its activation and is never respawned](#a-worker-lives-as-long-as-its-activation-and-is-never-respawned-2026-10-12) | 2026-10-12 |
| Nomos | [Nomos rejects a literal `rm -rf /` at intent validation, as an error](#nomos-rejects-a-literal-rm--rf--at-intent-validation-as-an-error-2026-10-12) | 2026-10-12 |
| Relay | [Relay gives a worker no caller identity, so one worker's conversations look alike](#relay-gives-a-worker-no-caller-identity-so-one-workers-conversations-look-alike-2026-10-13) | 2026-10-13 |
| Nomos | [The live Executor runs a writ at most once, and says so with a signed refusal](#the-live-executor-runs-a-writ-at-most-once-and-says-so-with-a-signed-refusal-2026-10-13) | 2026-10-13 |
| Nomos | [The live Council rejects certified evidence it cannot validate, as an error](#the-live-council-rejects-certified-evidence-it-cannot-validate-as-an-error-2026-10-13) | 2026-10-13 |
| Nomos | [This deployment accepts uncertified evidence](#this-deployment-accepts-uncertified-evidence-2026-10-13) | 2026-10-13 |
| identity | [Structured tool authorization needs a trusted subject boundary](#structured-tool-authorization-needs-a-trusted-subject-boundary-2026-10-13) | 2026-10-13 |

## Structured tool authorization needs a trusted subject boundary (2026-10-13)

Relay 0.10 gives the worker a tool name, arguments and tool-call id, but no authenticated PA user identity. The
plugin therefore cannot safely derive a multi-user subject from callback metadata, and model-provided arguments are
not an identity source. The structured local-tool contract accepts an operator-configured opaque subject and is safe
only when one worker activation serves that one subject, such as a per-user sandbox. A shared PA2 worker must wait for
an authenticated host-to-worker identity channel; silently using a service-wide or model-supplied subject would bind
the writ to the wrong security principal.

## R13 (new): Relay's codecs cannot patch back more than one edited tool per request

**What.** With a request codec active, H1 edits `annotated.tools`. If more than one tool changed in the same
request, `llm.execute` fails at encode with:

    invalid argument: cannot safely preserve provider fields for multiple edited array items without stable identities

**Why.** `align_changed_items` (NeMo Relay `crates/core/src/codec/mod.rs` ~196–245) matches edited array
items to the baseline by value. Unchanged items align; when more than one item is unmatched it refuses, before the
positional fallback, even if the order and length are unchanged. Tools have a stable identity, their name, but the
generic array patcher does not use it.

**Effect.** On the codec path, the plugin works for one guarded tool per request. Guarding several tools, or
`*`, breaks every model call that carries two of them.

**Workarounds.**
- H1 has a raw-body path, used when the host passes no request codec. It edits the OpenAI-chat `tools` directly.
- Keep one guarded tool per request on the codec path (the default config guards only `run_shell`).

**Proposed Relay fix (R13).** Align `tools` by function name before falling back to value matching:
[`relay-codec-align-tools-by-name.patch`](upstream/relay-codec-align-tools-by-name.patch) (597 codec tests pass at a9226978, 600 at 0.10.0, including 2 new; see [upstream](upstream/README.md)).

*Still holds at 0.10.0 (2026-10-12):* `align_changed_items` is unchanged; appending new items (the Nomos message)
is accepted alongside one edited tool.

*Fixed in Relay 0.11.0 (2026-10-30):* the codec path patches back several edited tools in one request; the plugin now
requires 0.11, so the workarounds above are no longer needed.

**Pinned by:** `tests/test_installed_worker.py::test_relay_codec_carries_two_edited_tools_in_one_request` (the installed
worker on the real host; on 0.10 the same request failed at encode).

## Plugin-signed evidence is not yet certifiable to Nomos

*Superseded 2026-10-30 (unreleased, #28, #29, #34):* no Nomos change was needed. Nomos certifies an
`ExecutionResult` from any registered, enabled trusted executor, so the plugin signs its own items as Nomos
`ExecutionResult`s with `signing_key`, under the executor id `nomos-relay-plugin:<fp16>`, and the operator registers
that key with the Council. With a `signing_key`, local tool results, Council denials and Council errors carry a
certified copy that a citation sends in `certified_evidence`; nothing is sent as asserted `evidence`, and refusals
and unknown outcomes are not cited. What still holds: a plugin certificate is the plugin's observation, not an
Executor run, and a later writ is not bound to it (Nomos's intent hash covers neither certified evidence nor
`context`).

**Pinned by:** `tests/test_local_results.py::test_a_cited_certified_local_result_goes_to_the_council_as_certified_evidence`,
`tests/test_council_decisions.py::test_a_cited_denial_goes_to_the_council_as_certified_evidence` and
`::test_nothing_is_ever_sent_as_asserted_evidence`, and, on the installed worker against Nomos's own check,
`tests/test_e2e_evidence.py::test_a_cited_denial_is_certified_and_passes_nomos_validation` and
`::test_a_cited_denial_from_an_unregistered_key_closes_the_gate`.

The original finding:

Nomos's `certified_evidence` accepts only Executor `ExecutionResult`s. It validates the Executor signature and the
durable writ record. Records the plugin signs for in-process tools are therefore sent as free-text `evidence`
(asserted), with the evidence id, output digest and key fingerprint. Accepting plugin-signed records would need a Nomos
change: a trusted plugin key and a writ-bound record type.

*Worked around (2026-10-30):* Nomos certifies any `ExecutionResult` signed by a registered trusted executor, and does
not look up a writ for it. The plugin now signs its local results, Council decisions and (opt-in) tool definitions as
`ExecutionResult`s under its own executor id, so once the operator registers the plugin's key every citation is
certified and nothing goes as asserted `evidence` ([DESIGN](../DESIGN.md#certified-council-decisions)). A Nomos
evidence type for the plugin's observations, separate from executor evidence, remains the real fix (limit 2).

## Certified evidence needs the Executor's key in the durable trust store

Citing an Executor result as `certified_evidence` fails with `Certified evidence executor is not registered: <fp>`
until the Executor's signing key is registered (`agent_accounts.py trusted-executor register`). Executors only
self-register in Redis. `trusted-executor register --from-redis` fails in the Kind chart at the pinned commit
with `invalid Redis database in URL: '//<ip>:6379/0'`.

**Root cause** (in the Kind chart): the Service is named `nomos-redis`, so Kubernetes injects the Docker-link variable
`NOMOS_REDIS_PORT=tcp://<ip>:6379` into every pod, and `default_redis_url()` prefers it over `COUNCIL_REDIS_PORT`.
Register each Executor key explicitly, or set `enableServiceLinks: false` in the chart.

**Pinned by:** `tests/test_e2e_evidence.py::test_a_council_that_does_not_trust_the_executor_key_is_a_closed_gate` (the
Council's error is a closed gate) and `tests/test_live_contract.py::test_live_the_council_validates_certified_evidence_it_is_sent`.

## The Executor has its own tool whitelist: approved does not mean executed

The Council approved `/bin/date` 3/3; the Executor then refused the writ (`success: false`,
`error: "Tool not allowed: date"`, `verification_stage: tool_whitelist`) and still returned signed evidence. A verified
signature proves who reported the outcome, not that the command ran: the plugin returns `execution_failed`, and the
result is certified as `not_executed`.

**Pinned by:** `tests/test_plugin.py::test_signed_refusal_from_the_executor_is_reported_as_not_run` and
`tests/test_e2e_evidence.py::test_a_signed_refusal_or_failed_run_is_certified_and_citable`.

## Standalone worker baseline: credentials, evidence and harness boundaries (2026-10-06)

*Superseded 2026-10-12 (plugin 0.3.0):* the harness evidence tools and `credentials_file` are gone (the catalog
and `{"file"}` references replace them), and since 0.4.0 the lease is gone too. What still holds: Nomos v1 executor
evidence verifies without importing the Nomos backend (canonical payloads match the pinned contract), and the Council
has no per-item citation acknowledgement, so the plugin records acceptance only after a valid signed approval.

**Pinned by:** `tests/test_verification.py`, `test_installed_worker.py`, `test_live_worker.py`.

## Relay 0.10.0: tool execution intercepts have no 30 s worker RPC deadline (2026-10-12)

At the old pin (0.10.0-alpha, a9226978) every worker RPC was bounded by `WORKER_RPC_TIMEOUT` (30 s), including a
tool execution intercept. That would have cut off H3 while a Council with LLM members deliberated. The 0.10.0 tag
(#1182, `crates/core/src/plugin/dynamic/worker.rs`, `invoke_async`) exempts `ToolExecutionIntercept` and
`LlmExecutionIntercept`: "their caller owns cancellation". The integration test bed's `vendor/nemo-relay` was then pinned to 0.10.0; this plugin now requires Relay
`>=0.11.0,<0.12` (`pyproject.toml`, `relay-plugin.toml`).

**Pinned by:** `tests/test_installed_worker.py::test_a_slow_council_is_not_cut_off_by_a_worker_rpc_deadline`
(opt-in, `NOMOS_TEST_SLOW=1`): a 35 s Council through the real host. It passed, with council timing ≥ 35000 ms.

## Relay strips a managed worker's environment, so `{"env"}` references cannot reach it (2026-10-12)

`minimize_worker_environment` keeps only `PATH`, `SYSTEMROOT`, `WINDIR`, `TMPDIR`, `TEMP`, `TMP`, `SSL_CERT_FILE` and
`SSL_CERT_DIR`, plus Relay's own `NEMO_RELAY_*`. There is no passthrough option in the manifest, policy or
`plugins.toml`. A plugin configured with `{"env": "NOMOS_AUTH_TOKEN"}` fails to start in a managed worker even when
the host has the variable set. The diagnostic names the variable and the reason. Use `{"file": …}` references.

**Pinned by:** `tests/test_installed_worker.py::test_worker_environment_is_stripped_so_env_references_fail_closed`.

## Relay rejects `writeOnly` under `anyOf` in a plugin config schema (2026-10-12)

`nemo-relay plugins add` refuses a schema with `writeOnly` inside or across `anyOf`:
`writeOnly must resolve directly … to type 'string'` and `writeOnly … is not supported under 'anyOf'`. Every leaf of
the plugin's schema is `anyOf: [<type>, <reference>]`, so secret fields cannot be masked by Relay's editor. Secrets
are supplied as file references instead, so `plugins.toml` never holds them.

**Pinned by:** the installed-worker tests, because `plugins add` validates the shipped schema, and by
`tests/qa/test_qa_settings.py`, which checks that the schema file equals `settings.SCHEMA`.

## `nemo-relay plugins add` pip-installs the bundle and ignores `uv.lock` (2026-10-12)

`crates/cli/src/plugins/lifecycle/environment.rs` (`provision_python_environment`) creates a venv and runs
`pip install <manifest_root>`. Dependencies are resolved afresh from the index; the bundle's `uv.lock` is not used.
On a slow network that took 4–5 minutes with many DNS retries. The tests can install from
a pinned offline wheelhouse (`scripts/wheelhouse.sh` → `PIP_NO_INDEX=1 PIP_FIND_LINKS=…`, one per Python
version). The CLI also keeps lifecycle state in `.dynamic-plugins.json` next to `plugins.toml`. Re-adding the
same plugin id fails with `already registered in the explicit lifecycle scope` unless that state is reset, so a
host that reinstalls on a bundle change must reset it.

**Pinned by:** the installed-worker tests (`NOMOS_TEST_WHEELHOUSE`).

## The live Council denied the former rendered non-command tool at its OPA stage (2026-10-12)

The previous implementation rendered a `local` (non-command) tool as the synthetic shell command
`read_file {"path":"notes.txt"}`. The Astra Council's deterministic stage returned
`Denied by OPA policy: Shell commands must invoke executables by absolute path; non-absolute command word(s):
read_file` before any member voted. That result motivated the native structured tool-call contract: the plugin now
sends the tool name and exact argument object in `tool_call`, while genuine `nomos_http` commands keep the existing
shell authorization flow. Live validation against a Council containing the paired Nomos change remains pending.

**Recorded in:** `docs/architecture/examples/local_tool.json`. **Pinned by:** the integration test bed's live tier.

## The live Council reasons over certified evidence (2026-10-12)

With a certified citation, the members read the original `ExecutionResult` and check the model's claim against it.
Asked to approve an echo whose justification claimed the cited evidence showed `ERROR: disk full`,
`command-integrity` and `environment-isolation` voted to deny: "the CERTIFIED EXECUTOR EVIDENCE directly
contradicts this: … exit_code 0 …". A cited echo whose string merely differed from the evidence was denied in two of
three live runs and approved in one. The certified item itself was accepted each time; an invalid one is a
`status: "error"`.

**Recorded in:** `docs/architecture/examples/live_contradiction.json` and `live_run.json`
(recorded by the integration test bed).

**Pinned by (2026-10-13):** the integration test bed's `test_live_the_council_reads_certified_content`
(best of three attempts; the claim was denied on the first attempt in 4 of 4 runs).

## A worker lives as long as its activation and is never respawned (2026-10-12)

`crates/core/src/plugin/dynamic/worker.rs` starts the worker process once, when the host calls
`plugin.initialize`, and kills it when the activation is torn down (`shutdown_checked`). There is no restart logic.
If the worker dies, the next guarded `tools.execute` raises
`RuntimeError: internal error: worker invoke failed: … Connection refused`. The intercept is not skipped, so the
tool does not run, and Relay does not start a new worker. Guarded calls keep failing closed until the host
initializes again.

This is what the plugin's in-memory evidence store (0.4.0) relies on. Evidence lives as long as one long-lived
worker. A dead worker loses its evidence, but it cannot be bypassed.

**Pinned by:** `tests/test_installed_worker.py::test_a_dead_worker_is_not_respawned_and_guarded_calls_fail_closed`.

## Nomos rejects a literal `rm -rf /` at intent validation, as an error (2026-10-12)

The pinned Nomos validates each intent with a pydantic `Intent` model before any policy or member runs. Its
"dangerous pattern" check (`rm\s+-rf\s+/\s*$`) rejects `/bin/rm -rf /` with `status: "error"`
(`Invalid intent: … Command contains dangerous pattern`), not with a denial. The plugin reports that as
`council_error`, the gate stays closed, and nothing runs. The same command with a trailing flag
(`/bin/rm -rf / --no-preserve-root`) passes validation and is denied by the Council's permission gate
(`missing_permissions: ["file:delete"]`). A harness that treats `council_error` as retryable should read
`council.retryable`, which is `false` here.

**Pinned by:** `tests/test_live_worker.py::test_live_command_citation_denial_and_restart`
(opt-in: `NOMOS_TEST_LIVE_CONFIG`).

## Relay gives a worker no caller identity, so one worker's conversations look alike (2026-10-13)

One activation (one `plugin.initialize` per host process) has one worker serving every conversation, and nothing
Relay sends identifies the caller:

| Relay sends | Source | Identity? |
|---|---|---|
| `tool_name`, `args`, `tool_call_id` | `nemo_relay_plugin/_api.py`, `ToolExecutionContext` | no |
| the app's `tools.execute` `metadata`/`data` | not in `ToolInvocation` (`plugin_worker.proto`) | not sent |
| a scope-stack id | `worker.rs`, `insert_invocation_scope_stack`: a new `invoke-<uuid>` per call | per call only |

Before the fix, a second conversation's catalog listed the first one's evidence, the Council judged a call against
the other conversation's user request, and a cross-conversation citation was approved and run. The plugin now uses
turn tokens (`DESIGN.md`, "Conversations sharing one worker"); a caller identity in Relay would be cleaner.

**Pinned by:** `tests/test_plugin.py::test_relay_gives_the_tool_hook_no_caller_identity`, and
the isolation tests beside it (`test_a_conversation_never_sees_or_cites_another_conversations_evidence`,
`test_the_council_gets_the_user_request_of_the_calls_own_turn`, and others).

## The live Executor runs a writ at most once, and says so with a signed refusal (2026-10-13)

Sending a writ the OpenShell Executor has already run returns HTTP 200 with `success: false`,
`verification_stage: "nonce"`, `error: "Nonce already claimed (replay detected)"`. The plugin relies on this for
`nomos_http` tools (its own ledger claims every writ too, but only within one worker's lifetime).

**Pinned by:** `tests/test_live_contract.py::test_live_the_executor_runs_a_writ_at_most_once`
(opt-in: `NOMOS_TEST_LIVE_CONFIG`).

## The live Council rejects certified evidence it cannot validate, as an error (2026-10-13)

Sent directly to the Astra Council, a certified item whose `stdout` was altered comes back
`{"status": "error", "error": "Certified evidence stdout hash does not match result stdout"}`, and one re-signed
with a key the Council has not registered comes back `"Certified evidence executor is not registered: <fingerprint>"`.
Neither is a decision. So when the live Council decides on a request carrying the plugin's certified citations, it
has validated each of them against its own executor registry. The plugin reports such an error as
`council_error`, and nothing runs.

**Pinned by:** `tests/test_live_contract.py::test_live_the_council_validates_certified_evidence_it_is_sent`.

## This deployment accepts uncertified evidence (2026-10-13)

*Superseded 2026-10-30 (unreleased, #34):* the plugin no longer sends asserted `evidence` (it is always `[]`), so a
Council with `COUNCIL_ALLOW_UNCERTIFIED_EVIDENCE=false` never rejects a request it sends for carrying asserted
evidence. Its certified items must still verify: the plugin's key must be registered as a trusted executor, and a
cited Executor result fails when the Executor's key is not. A cited denial, Council error or local result goes in `certified_evidence`;
refusals and unknown outcomes are refused `evidence_not_citable` without asking the Council. Pinned by
`tests/test_council_decisions.py::test_nothing_is_ever_sent_as_asserted_evidence` and
`::test_a_refusal_is_never_cited`. The original finding, which describes the behaviour before #34:

The test deployment runs the Council with `COUNCIL_ALLOW_UNCERTIFIED_EVIDENCE=true` (Nomos's default), so a
plugin-signed record (a denial, a refusal, a local tool result) cited by the model reaches the Council as asserted
`evidence` and is weighed. A Council configured with `false` returns an error for any request carrying asserted
evidence ("Uncertified evidence is disabled for this council; submit certified_evidence."), which the plugin reports
as `council_error`: models should then cite only certified records.

**Pinned by:** the integration test bed's `test_live_a_plugin_signed_denial_is_cited_as_asserted_evidence`.

## Structured calls need the same observed turn context as command calls (2026-10-14)

Local tools previously omitted `context` even though H1 had captured the call's own user request. Both
execution paths now submit that bounded request, with a marker when it was truncated. Another conversation's
newer request must never replace it. Nomos's structured API already accepts this common intent field.

**Pinned by:** `tests/test_review_regressions.py::test_local_context_is_from_the_calls_own_turn`,
`test_context_marks_truncated_user_request`, `test_implicit_local_policy_requires_subject`,
`test_signed_mismatch_is_a_stored_refusal_before_dispatch`, and
`test_changed_held_arguments_are_refused_before_callback`.

## Structured integer precision has an explicit portable bound (2026-10-14)

Python's decimal conversion limit previously rejected very large integers even when the structured arguments
fit the byte bound. Nomos and the plugin now explicitly accept at most 640 decimal digits per integer, excluding
the sign. Validation compares integers before decimal conversion, without changing interpreter-wide settings.
The accepted domain keeps the existing `nomos-json-v1` bytes and digest format.

**Pinned by:** `tests/test_review_regressions.py::test_integer_digit_boundary`,
`test_oversized_nested_integer_is_refused_before_authorization`, and
`test_invalid_changed_held_arguments_are_refused`.
