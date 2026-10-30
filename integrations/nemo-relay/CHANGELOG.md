<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Changelog

What changed for operators and integrators. Interface-level detail: [`DESIGN.md`, change history](DESIGN.md#change-history).

## Unreleased

- **Every citation is certified evidence; nothing is sent as asserted `evidence`.** With a `signing_key`, the
  plugin certifies each guarded local tool's result and each Council answer that authorized nothing (a denial, or a
  Council error), as a Nomos `ExecutionResult` signed with that key, under the executor id `nomos-relay-plugin:<fp16>`.
  A local result is bound to the tool, the arguments' digest and the verified writ; a decision carries only the
  answer's structured parts (status, guidance, missing permissions, retryable), never the members' or chairman's
  reasoning (which often quotes the agent), and no writ. A citation sends that certified copy in `certified_evidence`, after
  re-checking it against the plugin's key; `evidence` is always empty, so a Council with
  `allow_uncertified_evidence = false` accepts it. **Register the key with the Council as a trusted executor**
  (`scripts/agent_accounts.py trusted-executor register …`), or it refuses calls that cite these records. Refusals
  and unknown outcomes are never cited (`evidence_not_citable`). This certifies what the plugin saw (the
  application's tool output, the Council's answer), not an independent Executor run or a Nomos-signed decision.
  Each call in flight holds back room for a result three times (the record and its certified copy), so
  `limits.max_storage_bytes` must be larger. Without a `signing_key` nothing changes: nothing is stored or cited.

- **Opt-in: observed tool definitions for the Council, as certified evidence.** `tool_definitions = "called"`
  sends the Council, with every guarded call, the called tool's definition as the application offered it to the
  model, plus the digest of the whole tool list; `"all"` sends every definition (the Council shows the first 4000
  characters). It travels as one `certified_evidence` item: a Nomos `ExecutionResult` signed with `signing_key`,
  executor id `nomos-relay-plugin:<first 16 hex of the key fingerprint>`, that says no command ran. **Register that
  key with the Council as a trusted executor** (`scripts/agent_accounts.py trusted-executor register …`); a Council
  that has not registered it refuses the call. Nothing is sent as asserted evidence, so a Council with
  `allow_uncertified_evidence = false` accepts it, and `context` names the item's digest so the writ is bound to
  it. Needs `signing_key` (without one the option is ignored, with a `nomos.config.tool_definitions_disabled`
  warning). When on, the model may cite 19 evidence ids instead of 20. Tool lists count toward the storage quota,
  once store-wide and in the share of each conversation that keeps them; a list over `limits.max_citation_bytes`
  or the quota is not kept for that turn and is reported unavailable (the call goes ahead). The default (`"off"`)
  is unchanged. With it on, a call must name the turn of the request it answers (the definitions certified are that
  turn's): a turn a later request's messages showed is refused `unknown_turn`. Forks and concurrent requests of one
  transcript each keep their own turn.

## 0.5.0 (2026-10-14)

- **Structured local-tool authorization**: local tools send the exact argument object and operator-derived
  `tool_call.subject`; the Council derives the digest. Strict v2 writ verification binds the approved operation
  before callback dispatch. Replay claims remain worker-local; `nomos_http` keeps the command contract.

- **Opt-in: the Council's key from the Council.** `trust.council_public_key_source = "council"` fetches the key from
  `council_get_public_key` at `connection.mcp_url` on the first guarded call, instead of pinning
  `council_public_key_hex`. Fetched once per activation; refused calls (`council_key_unavailable`) until it arrives;
  needs https or loopback; `validate()` warns. The default (`pinned`) is unchanged.
- This repository: the plugin moved out of the Nomos × NeMo Relay integration test bed
  (`nomos_relay_playground` @ `324ae2a`), with its tests and documentation. The test bed keeps the unchanged agent,
  the dashboard and the cluster-level live tests, and installs this package.
- Package metadata: readme, SPDX license (`Apache-2.0`), classifiers and project URLs; the build backend needs
  `setuptools>=77`.
- Tooling: pre-commit (file hygiene, ruff, mypy, codespell, manifest and schema checks, rendered docs current,
  Markdown links), a branch-coverage floor in `make test`, CI on Python 3.11–3.13.
- Tests: Relay's R13 codec limitation is pinned in the installed-worker tier.

## 0.4.0 (2026-10-13)

- **In-memory evidence store**: SQLite `:memory:` inside the worker; no storage directory, no lease, no files.
  `session.storage_dir` is now a configuration error; retention defaults to one hour.
- **Conversations sharing one worker are isolated**: each request carries a turn token the model copies into
  `nomos.turn`; evidence, Council context, denials and quota are per conversation.
- **No signing key, no evidence**: without `signing_key` nothing is stored and the model is offered no catalog and no
  `evidence` field; Executor results are still verified before they are returned.

## 0.3.0 (2026-10-12)

- The evidence catalog in each model request replaces the harness evidence tools; `{"file": …}` value references
  replace `credentials_file`.
- The security audit's findings fixed, each with a regression test, except F12 (upstream: Relay ignores `uv.lock`)
  and F14 (an open question) ([audit](docs/security-audit.md#8-resolution-engineering-2026-10-12)).
