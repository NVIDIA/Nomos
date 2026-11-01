<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Nomos plugin for NeMo Relay (`nomos.council`)

`nemo_relay_nomos` is a NeMo Relay **worker plugin** that puts tool calls under a [Nomos](https://github.com/NVIDIA/Nomos)
council. Any application whose model calls go through `llm.execute` and tool calls through `tools.execute` is
governed with **no code changes**: install the bundle, add one `plugins.toml` entry.

![Who does what. The application process holds the application, its own tool and the Relay host. The plugin worker, a separate process, holds H1, which adds the intent field and a fresh turn token to every model request, H3, which verifies the writ and dispatches each tool call, and the in-memory evidence store. The Nomos deployment holds the Council, which decides, and the Executor, which runs commands. Only the Relay host sends the edited request to the model provider (C2). Numbered arrows: 1 Relay passes each model request to H1 (C3), which opens a turn in the store (C6); 2 each tool call to H3 (C3); 3 H3 asks the Council (C4), which answers with a signed writ or a denial; 4 after verifying the writ and claiming its nonce, an approved call runs either as a local tool back through Relay (C3), which runs the app's tool, or as a command on the Executor (C5); on a denial nothing runs; 5 the outcome is stored (C6); 6 only then does the result, or why nothing ran (the Council's reasons), go back to Relay (C3) and the application. Light arrows are replies. Box edges: dark blue the Relay host and plugin code, purple the model, green Nomos, teal the plugin's store, grey no Nomos code.](docs/figures/overview.png)

The plugin acts at two moments. **H1** sees every model request: it adds a required `nomos` intent field to each
guarded tool and a fresh turn token. **H3** sees every tool call: it checks the call, asks the Council, and runs an
approved call: a `local` tool through Relay in the application, a `nomos_http` command on the Executor. The Council
decides; the plugin never does. Evidence lives in the worker's memory only.

| For each guarded tool, the plugin… | |
|---|---|
| **requires an intent** | the model fills `nomos` (description, justification, target resource, blast radius) |
| **tells conversations apart** | each request carries a turn token the model copies into `nomos.turn`; each conversation keeps its own Council context and state, and cites only evidence its own transcript links to |
| **lists citable evidence** | id, tool and arguments of the conversation's earlier calls, never output |
| **asks the Council** | every citation goes as `certified_evidence`: Executor-signed originals, and the local results and Council decisions the plugin certified; nothing goes as asserted `evidence` |
| **acts on the answer** | approval: writ verified against the Council's key (pinned, or fetched once if the operator opts in), claimed, dispatched; denial: the full response is the tool result |
| **stores the evidence** | with a `signing_key`, every result, denial, error and most refusals are signed and stored before the model sees them; without one, no record is (the journal, turns and opt-in tool lists still are) |

More: [architecture](docs/architecture/README.md) (step by step, real data) · [`DESIGN.md`](DESIGN.md) (the contract the tests hold) ·
[findings](docs/findings.md) (Relay and Nomos behaviour it depends on) · [security audit](docs/security-audit.md) ·
[changelog](CHANGELOG.md).

## How it works

### What the plugin is connected to

![Every party and the eight connections between them. Inside the operator's host: the application process (application, app's tool, Relay host) and the separate plugin worker (H1, H3, writ handler, config loader, evidence store); secret files, the Relay CLI and the worker venv. Outside: the model provider and the operator's Nomos deployment (Council, Executor). Arrows start at the initiator and carry their connection id: C1 application to Relay, C2 Relay to the model provider, C3 Relay to H1 and H3 and the writ handler's next_ back to Relay, C4 H3 to the Council, C5 writ handler to the Executor, C6 the hooks to the store, and the once-only links: C3 Relay starts the worker, C7 config loader reads the secret files, C8 the Relay CLI installs the bundle. There is no Council–Executor link.](docs/figures/connections.png)

What crosses each connection (arrowheads in the figure show who initiates; the first party below initiates):

| | Between | Transport | What crosses it | When |
|---|---|---|---|---|
| **C1** | application → Relay host | in-process calls | `llm.execute(request)`, `tools.execute(name, args, tool_call_id)`; results back. The plugin is not on this link | every model call and tool call |
| **C2** | Relay host → model provider | the application's model call (HTTPS) | the request as H1 edited it (guarded tools require `nomos`; one trailing Nomos message holds the turn token and the catalog); the reply, possibly a tool call whose arguments carry `nomos`. The plugin never contacts the provider | every model call |
| **C3** | Relay host → plugin worker | Relay's worker protocol (gRPC, `grpc-v1`) | H1: model name and request in, edited request out. H3: tool name, arguments (with `nomos`) and `tool_call_id` in; the result plus a host-only `{"nomos": …}` annotation out. For a `local` tool the worker calls back through Relay (`next_`) with the arguments minus `nomos` | H1 on every model request, H3 on every tool call; the worker is started once |
| **C4** | H3 → Council | MCP over streamable HTTP, bearer proxy token | `council_authorize`: identity, the observed command or tool call, the call's turn's user request, the four intent fields, cited evidence, and (opt-in) the tool list the application offered; back: `approved` with a base64 writ and `writ_id`, or `denied` / `error` with reasons and votes. Opt-in, once: `council_get_public_key` | once per guarded call that passes the plugin's own checks |
| **C5** | writ handler → Executor | HTTP `POST /execute`, header `X-Agent-ID` | `{"writ": …}` unchanged; back: an Executor-signed `ExecutionResult`, read with a byte bound | once per approved `nomos_http` call, after the nonce claim; never for `local` tools |
| **C6** | worker ⇄ its store | SQLite `:memory:` inside the worker | turns, conversations, catalog reads, the call journal, records, writ claims, tool lists | every H1; up to three journal transactions per H3 call |
| **C7** | config loader (`values`) → secret files | file read: absolute path, owner-only, no symlinks, ≤ 64 KiB | tokens and the signing key, never logged | once, at worker start |
| **C8** | Relay CLI → bundle and worker venv | `plugins add` / `enable`: digest check, `pip install` | the bundle | once, at install |

The plugin's only network calls are C4 and C5. Neither the Council nor the Executor ever calls the worker, and the
writ travels Council → worker, and on to the Executor only for a `nomos_http` tool.

### What H1 does to every model request

![Sequence, time running down: 1 the application calls llm.execute (C1); 2 Relay passes model name and request to H1 (C3); 3 H1 adds the nomos field to guarded tools, the same bytes every time; 4 H1 opens a turn in the store and gets a new token (C6): in the one conversation the transcript's tokens show, or in a new one when they show none, or several (merged); 5 with a signing key, H1 reads the catalog (C6); 6 the edited request returns with one Nomos message holding the token and the catalog, never output (C3); 7 Relay sends it to the model provider (C2); 8 the reply is a tool call whose nomos.turn is the token (C2); 9 the reply reaches the application (C1)](docs/figures/h1.png)

The token changes on every request, so the transcript, not any caller identity, links a conversation's turns
([conversations](docs/architecture/02-request-path-h1.md)).

### What H3 does to a guarded tool call

Before anything may run: the plugin's own checks, the journal row (T1), the citations and the denial cap, then the
Council. Each mark on the H3 line ends the call there.

![Steps 10 to 17, time running down: tools.execute (C1) reaches H3 (C3); refused: invalid_arguments or missing_intent, the result showing the field to send; the turn is looked up (C6), refused if unknown or expired: unknown_turn, the result saying to call again with the latest turn; a second concurrent call of the conversation: session_busy; tool policy, trust snapshot or Council key fails. A line: none of these writes a journal row. 13 T1 writes the journal row authorizing (C6); the same tool_call_id and arguments again in this conversation return its stored outcome; a conflict, blocked conversation or full quota is refused by the store with no journal row and nothing stored. A line: every ending below is journaled at T3 (step 23) first. 14 the cited records, only from conversations this turn may cite, and the denial count (C6): a failed citation, a turn a later request has already shown (with tool_definitions on: unknown_turn) or the denial cap refuses without asking the Council; 15 the cited records are marked submitted_unknown (C6); 16 council_authorize (C4); 17 the decision, with a signed writ if approved; a denial (count + 1) or council_error (count kept): nothing ran, journaled at T3, and the model's tool result (C3) is the Council's whole answer without the writ: reasons, votes, missing permissions](docs/figures/h3-authorize.png)

Once the Council approves: the writ is verified locally, its nonce claimed (T2), then exactly one branch runs.

![Steps 18 to 26, time running down: 18 the writ is verified (the Council's key, this exact call, fresh), a failure refused and journaled at T3 with the denial count not reset; 19 T2 claims the nonce, then marks the row dispatching (C6), an already-claimed nonce is writ_replayed for any tool; then one of two branches: nomos_http, 20 POST /execute (C5) and 21 the Executor-signed ExecutionResult, its signature verified: executed or execution_failed; or execution_unknown (no, oversized or unreadable answer) or evidence_invalid (fails verification): the call may have run and is never retried; local, 20 next_ with the arguments minus nomos through Relay (C3) to the app's tool and 21 its output, executed or execution_failed (the tool raised), plugin-certified when there is a signing_key; 22 the writ was valid, so cited records become accepted and the denial count resets to 0 (C6); 23 T3 completes the row together with the record, and after an unknown outcome blocks the conversation (C6); 24 the tool result goes to Relay (C3) with a note only the host sees (the Council's votes, timings, trace); 25 the application gets the tool result (C1); 26 the model reads it on its next request](docs/figures/h3-dispatch.png)

Every ending reaches the application as an ordinary tool result. The model sees the result (`status`, the output or
the Council's reasons, and `evidence_id` when a record was stored); timings, the conversation id and the Council's votes on an approval go only to
the host, in the `{"nomos": …}` annotation ([what each side receives](docs/architecture/03-governed-tool-call-h3.md)).
A call that may have run (`execution_unknown`, `evidence_invalid`) is never retried: its conversation is blocked
until the worker restarts or the transcript's turn tokens have all expired ([what ends a block](docs/architecture/04-outcomes.md)).

Every ending and what it leaves behind: [every outcome](docs/architecture/04-outcomes.md). Who signs what:
[evidence and citation](docs/architecture/05-evidence-and-citation.md).

### The evidence store

![How long the evidence lasts: the store is SQLite :memory: inside the worker, empty at start and gone when the worker exits (Relay never respawns one; a new activation reads old ids as evidence_not_found); inside it a record is citable for one retention period R, then a tombstone (evidence_expired) for one more; a turn lives R, then unknown_turn; a completed call replays its outcome in its own conversation for R; writ claims last the worker's whole life](docs/figures/store.png)

[`repository.py`](nemo_relay_nomos/repository.py): no file, no service; rows expire with
`limits.retention_seconds` (1 hour by default).

| Table | Holds |
|---|---|
| `conversations` | denial count, blocked flag |
| `turns` | token, conversation, the user request sent as Council context, the digest of the request's tool list and whether it was kept (`tool_definitions` on) |
| `tool_lists` | each distinct tool list a live turn names (`tool_definitions` on) |
| `records` | results, denials, errors and refusals, whole (a `local` result over `limits.max_result_bytes` as a string bounded to `limits.max_output_chars`), with content signature and receipt (only with `signing_key`; passthrough, `session_busy`, replays and the store's own refusals store nothing) |
| `calls` | one journal row per (conversation, `tool_call_id`): a repeat is replayed, never re-run |
| `writ_claims` | nonces already used |

Three writes per call, each before the next exposure: journal (before C4) → claim + dispatching (before C5) → record
and outcome together (before the model). Details: [one call's data](docs/architecture/README.md#how-data-moves-through-one-call) ·
[H3 step by step](docs/architecture/03-governed-tool-call-h3.md) · [storage and recovery](docs/architecture/07-storage-and-recovery.md).

## Install and enable

```sh
nemo-relay plugins add --user ./Nomos/integrations/nemo-relay/relay-plugin.toml   # a checkout or release bundle: integrity check + private venv
nemo-relay plugins enable nomos.council
```

Policy override: bundles are integrity-checked, not signed; `startup = "required"` refuses to run tools ungoverned.

```toml
[plugins.policy.overrides."nomos.council"]
attestation = "integrity_only"
startup = "required"
```

## Operator configuration

The `config` table of the plugin's `[[plugins.dynamic]]` entry. A complete example:

```toml
[[plugins.dynamic]]
manifest = "/opt/relay/relay-plugin.toml"

[plugins.dynamic.config.connection]
mcp_url = "https://nomos.example.com/mcp"
executor_url = "https://executor.example.com"
agent_id = "agent-ops"
council_id = "council-prod"
working_directory = "/workspace"
auth_token = { file = "/run/secrets/nomos/auth_token" }
proxy_token = { file = "/run/secrets/nomos/proxy_token" }

[plugins.dynamic.config.guard]
include = ["*"]                 # every tool …
exclude = ["read_*", "search"]  # … except these

[plugins.dynamic.config.tools.run_shell]
executor = "nomos_http"          # the trusted Executor runs it; the app's function never does
command_argument = "command"

# Application-owned local tools automatically use structured authorization.
[plugins.dynamic.config.tools."slack--send_message"]
executor = "local"

[plugins.dynamic.config.tool_call]
# Host/operator-derived, never copied from model arguments. A static subject is
# suitable only when this worker activation serves one subject.
subject = { file = "/run/secrets/nomos/opaque_subject" }

[plugins.dynamic.config]
signing_key = { file = "/run/secrets/nomos/signing_key" }  # optional; without it no evidence is kept

[plugins.dynamic.config.session]
id = "ops-agent-2026-10-12"      # optional label; evidence is held in the worker's memory

[plugins.dynamic.config.trust]
policy_version = "ops-trust-2026-10"
valid_until = 1792022400
council_public_key_hex = "<64 hex: the Council's Ed25519 public key>"
[plugins.dynamic.config.trust.executors]
"openshell:939bbd39d2c6fffa" = ["<64 hex: the Executor's Ed25519 public key>"]
```

**Connection, identity and session:**

| Field | Default | Meaning |
|---|---|---|
| `connection.mcp_url` | `http://127.0.0.1:9901/mcp` | Council MCP endpoint (streamable HTTP) |
| `connection.executor_url` | `http://127.0.0.1:9907` | Trusted Executor base URL |
| `connection.agent_id`, `connection.council_id` | required | The agent's Nomos identity; never taken from the model |
| `connection.auth_token`, `connection.proxy_token` | `""` | Nomos agent token; Monitor proxy bearer token. **Use file references.** |
| `connection.working_directory` | `/workspace` | Directory the Council authorizes |
| `connection.timeout_s` | `180` | Per request to the Council or Executor |
| `connection.allow_insecure_http` | `false` | Required for `http://` to a non-loopback Council or Executor (tokens and evidence would travel in clear) |
| `session.id` | `activation-<random>` | A label for this activation's store. Evidence is held in memory and lasts as long as the worker. |
| `signing_key` | none | The plugin's Ed25519 seed (64 hex), normally a `{"file": …}` reference. Evidence is kept only with one: if omitted, no record or tool list is stored, the model is offered no catalog and no `evidence` field, `tool_definitions` is ignored, and `validate()` warns. The Executor's results are still verified before they are returned. With one, the key also certifies each local result and Council decision it stores, which is how they are cited: **register it with the Council as a trusted executor** (`nomos-relay-plugin:<fp16>`), or the Council refuses calls that cite them. |

**Guarding and execution:**

| Field | Default | Meaning |
|---|---|---|
| `guard.include`, `guard.exclude` | `["run_shell"]`, `[]` | Shell-style patterns. A tool is guarded if it matches an include and no exclude. `nomos_*` is never guarded. |
| `tools.<name>.executor` | `local` | `nomos_http` (the Executor runs it) or `local` (the app's function runs after approval) |
| `tools.<name>.command_argument` | — | For `nomos_http`: the single argument holding the command |
| `tool_call.subject` | — | Required for local tools. An opaque identity supplied by the host/operator, not the model. The static setting is safe only for a single-subject worker activation. |
| `default_executor` | `local` | For guarded tools not listed in `tools` |
| `writ_handler.kind` | `default` | `custom` is reserved, with `entrypoint` and `options` (not implemented) |
| `writ_handler.claim` | `auto` | The plugin's ledger always records each writ's nonce before dispatch; for `nomos_http` the Executor also claims it. `council` is reserved. |
| `max_denials` | `3` | Consecutive denials before the plugin stops asking |
| `catalog.enabled`, `catalog.max_entries`, `catalog.max_argument_chars` | `true`, `20`, `400` | The evidence catalog in model requests |
| `tool_definitions` | `"off"` | Send the Council the tool definitions the model was offered, as one **certified** evidence item signed with `signing_key`: `"called"` (the called tool's definition and a digest of the list) or `"all"` (every definition; the Council shows the first 4000 characters). Register the key with the Council as a trusted executor first (`nomos-relay-plugin:<fp16>`), or the Council refuses the call. Needs `signing_key`. When on, the model may cite 19 ids, not 20. Tool lists count toward `limits.max_storage_bytes`; one over `limits.max_citation_bytes` is not kept and is reported unavailable. See [DESIGN](DESIGN.md#observed-tool-definitions) |

**Trust and limits:**

| Field | Default | Meaning |
|---|---|---|
| `trust.*` | required | Pinned keys and a hard expiry; see below |
| `trust.council_public_key_source` | `pinned` | `council`: fetch the Council's key from `mcp_url` instead of pinning it; see below |
| `limits.*` | see [limits](docs/architecture/07-storage-and-recovery.md#limits) | Result size, memory budget, records, journal, retention (1 hour), citations, output, and each conversation's share of the store (25%) |

**Value references** (errors name the field and source, never the value):

| Form | |
|---|---|
| `{ value = … }` | literal |
| `{ env = "NAME" }` | in-process hosts only: managed workers start with a minimal environment |
| `{ file = "/abs/path" }` | owner-only (0600/0400), regular file, ≤ 64 KiB, one trailing newline removed |
| `{ encrypted = "…", key_env \| key_file = … }` | schema only: **not supported yet** (TODO) |

**Trust.** By default only operator-pinned keys: `council_public_key_hex` verifies writs; `executors` maps each
Executor id to its keys (several allow rotation). After `valid_until` (Unix time) nothing is trusted and nothing runs.
Provision keys yourself, never from a service's reply; restart the worker to rotate (its store starts empty).

**Fetching the Council's key instead** (opt-in): set `council_public_key_source = "council"` and leave
`council_public_key_hex` out. On the first guarded call the plugin asks `council_get_public_key` at `mcp_url`, checks the
reply names `council_id` and carries a well-formed key, and trusts that key for the rest of the activation (never
re-fetched, so a later reply cannot swap it). Until it has one, guarded calls are refused (`council_key_unavailable`)
and nothing runs. The trade: whoever controls that endpoint, or its TLS, controls approvals, so it needs `https://` (or
loopback) whatever `allow_insecure_http` says, and `validate()` warns. Executor keys and `valid_until` stay pinned.

```toml
[plugins.dynamic.config.trust]
policy_version = "ops-trust-2026-10"
valid_until = 1792022400
council_public_key_source = "council"   # no council_public_key_hex
[plugins.dynamic.config.trust.executors]
"openshell:939bbd39d2c6fffa" = ["<64 hex: the Executor's Ed25519 public key>"]
```

## What the application sees

Every governed result: `contract_version: "nomos-plugin/v1"`, `status`, `execution_status`, `evidence_status`, and
`evidence_id` when a record was stored; denials add `council` (the full response). The host alone also gets
`{"nomos": {...}}`: decision, votes, the submitted command or structured operation, citations, and timings. All outcomes:
[04 · outcomes](docs/architecture/04-outcomes.md).

## Requirements and limits

- NeMo Relay `>=0.11.0,<0.12` (tested on 0.11.0); Python 3.11+; Linux or macOS.
- No disk or extra service: evidence lives in the worker's memory and is lost if the worker dies.
- Guarded calls are sequential within a conversation; different conversations may run at once. The model must
  copy the turn token; a call without a live one is refused and taught.
- Structured local tools require the paired Nomos structured-operation API. Existing command tools continue to use
  the v1 command contract unchanged.
- The v2 path verifies the Council signature over the original payload bytes and rechecks the exact operation and
  argument digest before calling the application tool. Verifiers must not reserialize the signed payload.
  `nomos-json-v1` uses Python's numeric JSON encoding (including `1.0` and exact integers up to
  640 decimal digits per integer, excluding the sign),
  not RFC 8785/JCS. The paired clients are Python; other-language clients must reproduce this encoding.
  Its nonce claim is currently worker-local and is lost on
  restart; durable cross-worker consumption is intentionally deferred to Nomos's `council_consume_writ` API.
- A configured static `tool_call.subject` assumes one subject per worker activation. A shared multi-user host needs
  a future authenticated Relay caller-identity channel before enabling structured authorization.
- See [Limits and open items](docs/architecture/10-limits-and-open-items.md).

## Development

| Path | Holds |
|---|---|
| [`nemo_relay_nomos/`](nemo_relay_nomos/) | the package; [`worker.py`](nemo_relay_nomos/worker.py) is the manifest's artifact and entry point |
| [`relay-plugin.toml`](relay-plugin.toml) | the Relay manifest; this directory is the bundle root |
| [`tests/`](tests/) | implementation, security, installed-worker, end-to-end and opt-in live tests; [`tests/qa/`](tests/qa/) is the independent spec suite |
| [`docs/`](docs/) | architecture pages (rendered from [`docs/build/`](docs/build/)), findings, the security audit, an upstream patch |
| [`catalog/nomos-plugin/`](catalog/nomos-plugin/) | the entry for the NeMo-Relay-Plugins catalog (remote variant) |
| [`scripts/`](scripts/) | the test runner, the offline wheelhouse, the repository checks pre-commit runs |
| `../..` (Nomos) | this integration lives in Nomos's `integrations/nemo-relay`; the tests use the enclosing Nomos's own signer and certified-evidence validator |

```sh
make sync        # .venv from uv.lock (Python 3.11+, uv)
make test        # every offline tier, branch coverage of nemo_relay_nomos, fails under the floor in pyproject.toml
make lint        # every pre-commit hook of this integration over its files
make docs        # redraw docs/figures, re-render docs/architecture
make build       # sdist and wheel into dist/
```

`make test` installs the bundle through the real Relay CLI (`nemo-relay-cli-bin` 0.11 from the test group), offline
from a wheelhouse it builds on first use ([`scripts/wheelhouse.sh`](scripts/wheelhouse.sh)). The end-to-end tier uses
the enclosing Nomos (`NOMOS_TEST_NOMOS_SOURCE` points it elsewhere). `scripts/test.sh -k <expr>` runs a subset without the coverage floor;
`NOMOS_TEST_SLOW=1` adds a 35 s slow-Council test; `NOMOS_TEST_LIVE_CONFIG=<operator config> make test-live` runs
the live tier against a real Council and Executor. The tiers, and what each one simulates: [testing](docs/architecture/09-testing.md).
How to change things: [`CONTRIBUTING.md`](CONTRIBUTING.md).
