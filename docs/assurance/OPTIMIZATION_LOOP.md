<!-- SPDX-License-Identifier: Apache-2.0 -->
# Optimization loop implementation evidence

The richer exporter and offline commands are implemented on the follow-up branch.
See [the command guide](../../evals/nomos/README.md) and
[the reproducible authoring pilot](EVALUATION.md#evaluation-authoring-pilot).
The existing `/api/export` remains provenance-only. Rich export is a separate
opt-in operator capability and does not execute experiments in the live service.

A fresh native authoring proof ran October 28, 2026, with Eval Author
`20f95a0a010d4905c13065678af51970404910c9`, Harbor 0.23.0 and Python 3.13.10.
The selected capture came from ordinary disposable council proposals, verified
signed ledger records, and the operator preview/download APIs. Partial council
captures remained unsupported for client trace derivation. The test secret was
absent from the downloaded archive. A separate synthetic ATIF fixture was accepted
by Harbor's native trajectory reader; it is not an observed client interaction.

| Native control | Reward | Runtime exception | Proof |
| --- | --- | --- | --- |
| reference | 1.0 | None | passed |
| nop | 0.0 | None | passed |
| incorrect-scope | 0.0 | None | passed |
| alternative | 1.0 | None | passed |

These controls establish the export-to-manual-task/verifier handoff. The offline
workflow tests separately exercise real council orchestration using a deterministic
member, independent grading, fresh signing/ledger identities, budget reservations,
resume checks, manifest mutations, split gating, per-task safety gates and scored
Compass trace validation. Neither type of control measures model quality.

The model-backed pilot is **pending provider/model selection and a declared call
budget**. No model-backed baseline/candidate result, improved behavior or promotion
is claimed. The current pilot decision is **inconclusive: performance unmeasured**.
Client recovery is deferred because no fixed real client harness is supplied.

The first release runs one instruction candidate per frozen experiment. Rejected
or inconclusive runs can finish without exposing held-out evidence. Further
revisions require another reviewed freeze and an external maximum of three total
candidate revisions; exposed holdouts must be replaced. The task owner still
needs to review a substantive suite and its split before a provider-backed pilot.

Offline Compass tool diagnosis and scored trace normalization are implemented.
ETHOS and grader-failure signals accept separately configured bounded inference
and are reported as skipped when it is absent. Their actual pinned entry points
receive scored traces and the frozen contract in regression coverage; model-backed
diagnosis remains pending the pilot environment. Eval Author coverage review remains an explicit human
workflow with recorded source mappings and a next-experiment backlog; generated
coverage-review inputs are not a claim that the human review has happened.

The Harbor agent/worker interface is implemented separately from ordinary startup.
The native task controls above run in Harbor; provider-backed execution of the
reusable council agent still requires a configured private environment image.
The local manifest runner uses the shared council adapter directly.

No automatic deployment, production candidate modification, or live-service
optimization worker is included. Promotion remains normal configuration/code
review after complete final evidence. This document records current evidence and
remaining acceptance work; it does not assert completion of every pilot criterion.

Task proof manifest SHA-256: `3b219e615c812c411ebbb1060474a5b3e99a71b0d0eeadb6e9f471ad2be7c0e7`.
