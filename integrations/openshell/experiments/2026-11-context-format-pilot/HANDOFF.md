<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Resume this work on another machine

This handoff is designed to work without the original VM. No credential,
private deployment configuration, model weight, or internal operational runbook
is included. Obtain API/repository access through your normal private mechanism.

## Continuation update: expanded three-condition run complete

The follow-up was expanded to 96 cases (48 existing, 48 new) and freshly assessed
in three conditions: Original + compact context, C + compact context, and
Original questions with HTTP state only. All 288 calls succeeded. Start at the
[current report](../2026-11-context-guidance/RESULTS.md) and its
[reproduction guide](../2026-11-context-guidance/README.md). Current raw evidence
is `data/measured-v2`; the preceding 48-case/two-condition evidence remains in
`data/measured-v1` and is not pooled into current tables. Do not rerun either.

Observed correct decisions were 85/96, 83/96 and 94/96 respectively. Context with
original questions mainly introduced false blocks on this outbound-decidable
set; C reduced false blocks but introduced harmful allows. This does not test
context's benefit when it supplies genuinely necessary missing information.
Do not adopt C on this evidence; no formatter/question deployment changed.

## What is complete

The [results](RESULTS.md) and [interactive report](report.html) describe a completed
12-fixture component/model pilot: 144 comparison cells, 126 measured Jev calls,
and a separate excluded connectivity probe. It used the real context API and
native TLS/JWT middleware reader, followed by serial direct Jev replay.
It did not execute Hermes tools or live Council decisions. The publication
records were synthetic and went through the normal authenticated API.

Original questions plus compact JSON matched all 12 fixture labels. Expanded
questions introduced a false block; a diagnostic revision introduced a harmful
escape. No format or instruction revision has been deployed by this experiment.
Compact JSON is the candidate for the [next controlled study](PLAN.md), not a
new production formatter already installed in the reader.

Both tool-definition bookkeeping fields have now been removed from the Relay
publisher's new snapshot payloads. They remain internal to the worker. The
context-service README example matches. Historical pilot inputs retain their
original fields: never rewrite evidence to resemble later code.

## Repositories and branches

| Work | Repository / branch | Review |
| --- | --- | --- |
| Completed pilot, evidence, plan and this handoff | [NVIDIA/Nomos](https://github.com/NVIDIA/Nomos), `feat/context-format-pilot` | [PR #144](https://github.com/NVIDIA/Nomos/pull/144) |
| Reader implementation | NVIDIA/Nomos, `feat/openshell-context-reader` | [PR #141](https://github.com/NVIDIA/Nomos/pull/141), based on `feat/openshell-middleware` |
| Context service and updated snapshot example | NVIDIA/Nomos, `feat/conversation-context` | [PR #140](https://github.com/NVIDIA/Nomos/pull/140), targeting `develop` |
| Relay publisher and bookkeeping-field removal | [NVIDIA-dev/nomos-plugins](https://github.com/NVIDIA-dev/nomos-plugins), `feat/openshell-context-publishing` | [PR #32](https://github.com/NVIDIA-dev/nomos-plugins/pull/32), targeting `main` |

The experiment branch is stacked on the reader branch. The context service is
on a different branch, so use a separate clone/worktree; its source is not
present in the experiment checkout. Check PR states before rebasing or merging.
No PR was merged as part of this checkpoint. Immutable historical source hashes
are recorded in the measured manifests; branch tips may subsequently advance.

## Download and inspect without models or Docker

```sh
git clone --branch feat/context-format-pilot https://github.com/NVIDIA/Nomos.git nomos-context-study
cd nomos-context-study/integrations/openshell/experiments/2026-11-context-format-pilot
python3 -m unittest discover -s tests -v
python3 build_report.py \
  --initial data/measured-v1/results.json \
  --iteration data/measured-v2/results.json \
  --output report.html
```

Open `report.html` in a browser. Python 3.11+ is enough for these commands.
No API key is needed, and no network model calls are made.

Tracked `data/` includes all 126 measured request/response pairs, individual
measurements, comparison matrices, the component capture proof/states, frozen
source inputs and freeze hashes. Exact duplicate cells refer to one measurement.
The original `cases.json` scope sentence was clarified after collection; the
verbatim measured source is preserved in `data/input-snapshot/`. See
`data/provenance-notes.json`; fixture content and measured requests did not change.
Private host diagnostics and unrelated agent-provider probes are intentionally
not part of this public evidence package.

## If recapturing the real components

Follow [the reproduction guide](README.md) and the context-service branch's
README. Clone `feat/conversation-context` separately, then run `uv sync --frozen`
inside `integrations/conversation-context` to install its service environment.
Build the reader with `cargo build --locked --bin nomos-request-review` from
`integrations/openshell`; its toolchain is documented in the integration README.

The optional capture helper needs `grpcio`, `protobuf`, `cryptography`, `PyJWT`
and `httpx` in its Python environment. Versions actually used are preserved in
`data/capture/capture-proof.json`; install those exact versions in an isolated
venv if reproducing capture. It also needs a `protoc` executable with its matching
`include` directory at `protoc.parent.parent / "include"`. Use the vendored Cargo
protoc distribution from the build rather than assuming a system layout.
Pass explicit paths via `capture_context.py --help` as shown in README.

The capture script starts only temporary loopback processes, creates temporary
TLS/authentication material and removes it afterward. It does not launch an
OpenShell gateway, restart a service or call Jev. Fresh certificates/IDs/times
will differ; historical raw capture is preserved for exact replay.

For future live Hermes testing, read [readiness boundaries](READINESS.md).
Do not present component fixtures as live Hermes/Relay/Council validation.

## Validation at this checkpoint

The study has ten offline contract/report tests. The publisher change passed
858 tests with 93.05% branch-inclusive coverage and four explicitly opt-in tests
skipped, using the managed Python 3.11 environment (`UV_PYTHON=3.11 make test`).
The system Python environment lacked `ensurepip`; selecting the supported
managed interpreter resolved the installed-worker test setup failure. Applicable
repository lint/type/security/documentation checks passed.

## Next actions for Codex

1. Read this handoff and the expanded follow-up RESULTS/README before running
   anything. Check PR #144 and the remote branch tip rather than assuming the
   local checkout is current.
2. Both studies are complete. Expanded evidence is `data/measured-v2` under
   `2026-11-context-guidance`: 96 cases, 288 responses and 288 finished markers.
   Preserve the earlier `measured-v1` evidence too. Do not overwrite or rerun.
3. Explain the three conditions precisely: original questions plus compact
   context; the same plus short prefix C; and original questions with HTTP state
   only. The report does not recommend C or establish that context has no value.
4. Summarize these results and wait for the user's next requested experiment or
   implementation. A later run needs a new plan/output directory and review;
   do not treat the historical run authorization as an instruction to repeat it.
5. If a new run is explicitly requested, fetch `JEV_API_KEY` privately. Never
   copy shell startup files, print keys, store Authorization headers or include
   environment dumps in reports.
6. Keep reports clear about synthetic records versus real agent execution,
   selected author labels, related families, cache uncertainty and production
   deadlines. Do not merge PRs or alter unrelated deployed services.

Suggested prompt for the new Codex session:

> Read integrations/openshell/experiments/2026-11-context-format-pilot/HANDOFF.md
> and PLAN.md on feat/context-format-pilot. Check remote work before rerunning.
> Both studies are complete; the follow-up now has 96 cases and 288 calls. Read it at
> integrations/openshell/experiments/2026-11-context-guidance/RESULTS.md.
> Summarize the current results and ask what work to do next; do not rerun either
> measured study. Preserve evidence and publish no secrets or internal-only information.
