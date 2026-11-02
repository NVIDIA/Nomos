<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Resume this work on another machine

This handoff is designed to work without the original VM. No credential,
private deployment configuration, model weight, or internal operational runbook
is included. Obtain API/repository access through your normal private mechanism.

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
| Completed pilot, evidence, plan and this handoff | [NVIDIA/Nomos](https://github.com/NVIDIA/Nomos), `feat/context-format-pilot` | New draft PR linked from the branch after publication |
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

1. Read this file, RESULTS and PLAN before running anything.
2. Check whether the next-study branch/folder already contains collection
   evidence. Resume from its manifest; never overwrite or silently redo a run.
3. If not collected, implement the 48 new cases and control/C runner per PLAN,
   obtain focused review, then run the authorized 96 calls. No old regression
   examples and no third instruction variant are requested.
4. Use compact JSON; keep original questions and 0.70 threshold. The only
   experimental change is the short prefix when context is present.
5. Fetch `JEV_API_KEY` privately. Never copy shell startup files, print the key,
   save Authorization headers or include environment dumps in reports.
6. Publish a separate newcomer-friendly results report and standalone HTML with
   setup, design, worked examples, exact counts, failures and interpretation.
7. Push reviewed code/evidence and verify remote commit IDs. Do not merge PRs
   or alter unrelated running gateways/sandboxes.

Suggested prompt for the new Codex session:

> Read integrations/openshell/experiments/2026-11-context-format-pilot/HANDOFF.md
> and PLAN.md on feat/context-format-pilot. Check remote work before rerunning.
> Continue the authorized control-versus-C compact-context study with 48 new
> examples and 96 calls, focused subagent review, and a clear public report.
> Preserve the old pilot and publish no secrets or internal-only information.
