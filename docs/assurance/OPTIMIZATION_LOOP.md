<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
# Optimization loop implementation evidence

The protected rich exporter, offline lifecycle and native Harbor integration are
implemented. See the [command guide](../../evals/nomos/README.md),
[user-approved pilot contract](OPTIMIZATION_PILOT_CONTRACT.md),
[coverage review](OPTIMIZATION_COVERAGE_REVIEW.md) and
[authoring proof](EVALUATION.md#evaluation-authoring-pilot).
The existing `/api/export` remains provenance-only; the live service runs no
optimization workers.

## Completed local evidence

The [recorded native authoring proof](AUTHORING_PILOT_RECEIPT.md) is dated
October 27, 2026 and used pinned Eval Author
`20f95a0a010d4905c13065678af51970404910c9`, Harbor 0.23.0 and Python 3.13.10.
Normal disposable council proposals produced signed ledger records and a protected
selected-evidence export. Missing client inputs remained missing; a test secret
was absent from the archive. Native reference/alternative controls earned 1 and
NOP/incorrect-scope controls earned 0. The separate synthetic ATIF format check
establishes compatibility only, not an observed client conversation.

The expanded six-example suite passed 36 native verifier controls: Oracle, NOP,
targeted-failure, approve-all, deny-all and valid alternative on each case.
All controls matched their expected rewards; blanket policies fail the designated
opposite-action cases. These are task proofs, not model performance measurements.
Private receipts preserve exact verifier and image hashes. The subsequent isolated
worker image excludes task verifiers and labels. Two simultaneous native council
wiring checks passed after unique Docker project identities were introduced.
Those checks use a local fixed-response HTTP fixture, with no external inference.

The staged workflow supports a baseline-only freeze, development diagnosis,
immutable candidate attachment, development gates, candidate freeze and final
comparison. Native trials receive one request/instruction, never the suite or
expected labels. Budgets reserve requests before dispatch; failed/interrupted
attempts remain recorded. Two infrastructure errors stop further dispatch.

## Model-backed pilot outcome: rejected

The completed [pilot report](OPTIMIZATION_PILOT_REPORT.md) records 24 trials with
no infrastructure errors on four controlled cases, three repetitions per arm.
Development gates passed. Held-out comparison rejected the candidate for two
out-of-contract resource approvals and must-pass failures, despite aggregate gain.
A positive held-out case has a prose/command mismatch; its refusals are confounded
and its frozen label was not changed after measurement. The resource violations
independently support rejection. No shipped council defaults were changed.

The working hub endpoint is `https://inference-api.nvidia.com/v1`, with Bearer
`INFERENCE_HUB_KEY` and catalog route `nvidia/nvidia/nemotron-3-super-v3`. Council
trials and Compass used the same configuration. A completed five-call development
diagnosis reported no grader-failure patterns or ETHOS problems. These advisory
empty results do not establish absence of problems. The candidate addresses the
independently measured missing correction format.

Earlier access failures, calibration errors, failed diagnostic attempts and an
inconclusive experiment remain recorded separately. After correcting an implicit
agent timeout to honor the declared 120-second window, a new experiment used fresh
trials. Its development diagnosis explicitly references a reviewed compatible
prior analysis; the failed repeat diagnosis remains preserved. The report details
this reuse and its implementation delta.

Cumulative reservations stopped at the user-approved ceiling of 87, including
probes, errors and diagnosis. No reservations were refunded. Monetary cost and
overall provider token usage are unavailable. The sanitized receipt records known
trial usage, artifact hashes and gates. The
[reusable reviewed suite](../../evals/nomos/suites/pilot-20261028/README.md)
includes the controlled source bundle, reviewed definition and reproduction route.
The repaired four-case suite also passed all 24 native verifier controls against
its exact runtime image and verifier; these controls use no model calls.

## Acceptance and next experiments

The bounded export-to-task, baseline, diagnosis, candidate, final comparison and
coverage-review workflow has completed with a sound rejection. This establishes
workflow feasibility, not a successful optimization or representative safety
benchmark. The [coverage review](OPTIMIZATION_COVERAGE_REVIEW.md) maps actual
observations to the reviewed audit specification and records unmeasured areas.
The council-only evidence has no genuine client ATIF, so automated ATIF coverage
measurement is not claimed.

Reject this candidate for promotion. Before another experiment, align the positive
fixture's prose and command, review new labels and controls, and replace the now
exposed final family. Natural-language guidance usefulness, real client recovery,
actual execution, production OPA enforcement and multi-member behavior remain
outside this pilot. Production promotion remains a normal reviewed change.
