<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Contributing

This integration is its own project inside Nomos (`integrations/nemo-relay`): its own `pyproject.toml`, `uv.lock`,
tests and hooks. Set up with `make sync`, from this folder; `make test` and `make lint` must pass before a change is
merged. `make lint` runs this folder's `.pre-commit-config.yaml` from Nomos's root over this folder only; Nomos's own
hooks leave it to this one.

## Rules

| Change | Also do |
|---|---|
| any behaviour | a test that fails without it; `DESIGN.md` if a promise, interface or limit changes |
| `nemo_relay_nomos/worker.py` | the `integrity` digest in `relay-plugin.toml` (the `plugin-integrity` hook fixes it) |
| `settings.SCHEMA` | `config.schema.json` (the `plugin-schema` hook regenerates it) |
| code a page quotes, a template, an example | `make docs`; the `docs-current` hook re-renders `docs/architecture/` |
| the version in `pyproject.toml` | `uv lock` (the catalog refuses a lock that records another version), `CHANGELOG.md`, `catalog/nomos-plugin/release.toml` |
| new Relay or Nomos behaviour | an entry in [`docs/findings.md`](docs/findings.md) with the test that pins it |
| a fix Relay or Nomos needs | a patch in [`docs/upstream/`](docs/upstream/), verified in a scratch `git worktree` of the upstream repository, with the command and result in its README |

- **`tests/qa/` is the independent spec suite**, written from `DESIGN.md` without reading the code. Change it only to
  apply a change to the spec, never to make it pass.
- **The tests use the enclosing Nomos** (the repository root) for its contract, signer and certified-evidence check,
  so a Nomos change that breaks the contract fails here too. `NOMOS_TEST_NOMOS_SOURCE` points them at another checkout.
- **No secrets in the repository.** Tests use generated test keys; operator configs for the live tier stay outside
  the checkout (`NOMOS_TEST_LIVE_CONFIG`).
- Every source and documentation file carries the SPDX header (the `plugin-conventions` hook checks it).
