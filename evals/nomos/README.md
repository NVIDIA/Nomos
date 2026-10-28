<!-- SPDX-License-Identifier: Apache-2.0 -->
# Offline council optimization

The deployed assurance service only captures evidence. Run this workflow from a
separate Python 3.13 checkout with `uv sync --frozen --extra all --extra assurance-compass`.
Optional Harbor execution uses `requirements.txt` in its own environment. Eval
Author is pinned to `20f95a0a010d4905c13065678af51970404910c9`; Compass to
`518ad41d583e259205fb89623cb035ae3904f3f8`; Harbor to 0.23.0. The
[authoring guide](../../docs/assurance/EVALUATION.md#evaluation-authoring-pilot)
contains the tested downloaded-bundle-to-task route and native control commands.

Keep bundles and trials under `.runs/nomos-optimization/` and authoring under
`.eval-author/`; both are ignored. These directories contain private evidence,
prompts, and disposable signing keys. Never publish them. Delete a completed
private directory when its evidence-retention period expires.

## Commands

Run from the repository root. Commands are owned by Nomos.

```sh
python -m evals.nomos intake capture.zip .runs/nomos-optimization/intake
python -m evals.nomos freeze reviewed-experiment.json .runs/nomos-optimization/experiment
python -m evals.nomos run .runs/nomos-optimization/experiment --split development
python -m evals.nomos compare .runs/nomos-optimization/experiment --split development
python -m evals.nomos analyze .runs/nomos-optimization/experiment --split development
python -m evals.nomos freeze-candidate .runs/nomos-optimization/experiment
python -m evals.nomos run .runs/nomos-optimization/experiment --split held-out
python -m evals.nomos compare .runs/nomos-optimization/experiment --split held-out
```

Intake checks every archive member and exported hash before writing. It retains
the original ZIP privately and creates `intake.json`. Findings begin deferred;
the task owner must record accepted/duplicate/deferred/unsupported/rejected status,
reason, task IDs, missing fixtures and contradictory evidence before suite review.
Transformed hashes attest export consistency, not original ledger signatures.

The `Experiment` model in `workflow.py` defines the strict manifest. Required
inputs include a hypothesis, contract, named reviewer, source capture hash and an `intake_review` recording every selected finding,
tasks with source finding/rationale/family/split/expected decision, a baseline instruction (and optionally a predeclared candidate), provider model and endpoint, repetitions, budgets, version identities
and proof results from `prove(tasks)`. No source trace becomes a shell command.

Freeze stores an identity over the reviewed manifest and all Python implementation
files in the council, contracts and evaluation packages. Any subsequent code or
manifest change requires a new experiment. The manifest includes both splits;
keep it in the final evaluator's private workspace. Give the candidate designer
only `design.json`, which excludes held-out requests and labels. Filesystem access
separation is the operator's responsibility. No claim of enforced secrecy against
an operator who can read the final workspace is made.

This implementation supports one candidate revision per experiment. Freeze the
reviewed suite, measure the baseline, diagnose development evidence, then attach
the instruction-only candidate. If rejected, stop or start another reviewed experiment within an
external maximum of three revisions. Do not tune against final outcomes. Retire
any exposed final cases before a subsequent edit.

`run` uses the real `CouncilService.propose_intent` orchestration with one member,
fresh local keys, ledger, agent and council identities each trial. Clear deployment `COUNCIL_*` variables before running;
the adapter refuses them before creating any state. Signing and storage modes are
explicitly local. Its narrow
fixture configuration disables OPA and has no executor or deployment capabilities.
It measures council decisions only, not authorization enforcement, guidance
usefulness, client recovery or completed actions. The sole arm difference is the
member instruction. Calls go to an operator-selected OpenAI-compatible endpoint;
set `NOMOS_EVAL_API_KEY` privately. Provider environment/proxy configuration is not
inherited by the HTTP client. Temperature is zero; output tokens and wall time
are bounded. There are no automatic model retries. Provider errors and malformed output
(including missing reasoning) are errors even if Nomos fails
closed. Actual usage is retained when reported; missing usage stays unavailable.

Resume by repeating `run`. It checks frozen identities and preserves all existing
attempts, including errors. A reservation is saved before each call. Interrupted
trials consume their conservative reserved call/time budget and are never replayed;
start a fresh experiment after reconciling them. The process lock excludes two
concurrent runners. Baseline/candidate order alternates across tasks/repetitions.

The comparator retains missing/error denominators and rejects any candidate
must-pass failure or unsafe approval before considering gains. Development gates
must pass before `freeze-candidate`; final gates require a predeclared positive
gain on held-out tasks as well. Three repetitions establish feasibility, not
statistical significance. There is no deployment command or automatic promotion.

`analyze` writes validated pinned Compass traces with trial/case/arm identity,
independent `nomos.decision-v1` scores and original ledger references. Offline tool
signals are diagnostic. ETHOS and grader-failure inference remain explicitly
skipped without their separate judge/contract prerequisites. `coverage-review.json`
is an input for the pinned Eval Author coverage skill and a human task owner; it
is not a claim that Eval Author has completed that review. Record the resulting
coverage judgment and next-task hypotheses separately. Repetitions do not create
additional independent task coverage.

## Harbor adapter

`evals.nomos.harbor_agent:NomosCouncilAgent` supports Harbor 0.23.0's agent
interface. Configure a private environment image with this checkout at
`/opt/nomos`, its dependencies, a writable `/trial` and `/app`, and an
operator-installed `/experiment/trial.json` containing minimal `settings`,
one `request`, and one `instruction`, without task labels or the full suite. Inject the provider secret through the environment
secret configuration. The fixed worker ignores instruction prose and writes
`/app/decision.json` for an independent verifier. Never put the verifier or oracle
in the agent image. The environment must be destroyed after every trial.

The native Harbor executor and worker share `adapter.trial`; the manifest runner
owns cross-trial accounting and the persistent request budget. Standalone worker
invocations do not replace that lifecycle. Native controls are reproducible without
provider access. The completed [pilot](../../docs/assurance/OPTIMIZATION_PILOT_REPORT.md) rejected
the candidate; all 24 repaired trials completed without infrastructure errors.

For optional score-aware Compass diagnosis, add `--inference-config private.json`
to `analyze`. That file uses `InferenceConfig` fields `model` (an explicit
`openai/` or `nvidia_nim/` name), `api_base`, `max_calls`, `max_tokens`, and
`request_timeout`. Supply the credential separately as
`NOMOS_EVAL_ANALYSIS_API_KEY`. The pinned grader-failure signal receives independent
scores and the ETHOS signal receives the frozen experiment contract. Both share
one declared request budget; individual requests and whole signal calls have
wall-time bounds. Without this configuration, both signals are explicitly skipped.
Tool analysis receives the same scored snapshot and may report `No tool calls`
for council-only captures. Signal findings remain diagnostic, never graders.

## Acceptance pilot and staged candidate selection

The [user-approved pilot contract](../../docs/assurance/OPTIMIZATION_PILOT_CONTRACT.md)
defines the bounded subject, independent correction check and limitations.
`Experiment.candidate` may now be absent at the initial freeze. Run baseline
trials with `run DIRECTORY --arm baseline`, then analyze development evidence.
`attach-candidate DIRECTORY instruction.txt` requires complete baseline trials
and a recorded development diagnosis; a failed inference diagnosis blocks
selection. It records the candidate hash and diagnosis hash separately from the
immutable suite. Run `--arm candidate` next. Existing experiments that freeze both
arms together remain supported.

`scripts/assurance_optimization_pilot.py` coordinates the controlled example in
explicit stages: `prepare`, `freeze`, `baseline`, `diagnose`, `design`, `candidate`,
`freeze-candidate`, `final`, `compare`. Supply `--root` in the ignored private
runs directory, `--harbor-python` pointing to Harbor 0.23.0, and `--image` as the
immutable image ID built from `evals/nomos/Dockerfile`. `prepare` captures normal
signed council evidence and authors labeled fixtures. Model stages read the selected credential variable from `--env-file` (default
`deploy/.env`); the deployment environment is never sourced. The completed hub
pilot used the same hub model and credential for both council trials and Compass,
as described below. Earlier public-endpoint attempts remain separate experiments.

The native `HarborExecutor` now connects the manifest runner to Harbor. Each
container receives one request, one instruction and bounded runtime settings;
it receives neither the suite nor expected answers. Harbor injects verifier-owned
labels and grading separately. Native results, collected signed ledger records,
model interactions and runtime image identities remain in the private trial
directory. Unique Docker project names prevent independent runs sharing cleanup
or state. Native execution reserves the declared worker window plus at most 120 seconds
for setup, verification and collection. The worker retains its own declared
deadline. Cancellation asks Harbor to finish cleanup, with a bounded host wait.

For native verification without external inference:

```sh
python scripts/assurance_optimization_smoke.py \
  --out .runs/nomos-optimization/new-native-smoke \
  --harbor-python /path/to/harbor/bin/python
```

The smoke builds the pinned runtime image and runs real council orchestration
inside Harbor against a local fixed-response HTTP fixture. It proves wiring and
correlation, not model quality. `scripts/assurance_optimization_controls.py` runs
Oracle, NOP, targeted-failure, approve-all, deny-all and valid-alternative controls
for every reviewed task using native Harbor verification, with no provider calls.
Failed proof attempts and interrupted model reservations remain in their original
output directories. Use a fresh controls output directory; never overwrite receipts.

Compass diagnosis accepts the same persistent `RequestBudget` as the native
executor. Failed diagnoses retain their error type and reserved call count, and
each diagnosis attempt is preserved. Scored traces remain available for inspection.
A model appearing in the provider catalog does not establish that the credential
can invoke it; report inference access failures separately from model behavior.

## Selecting the hub provider

The hub uses provider-specific model IDs. The requested public-catalog ID
`nvidia/nemotron-3-super-120b-a12b` was absent from its catalog and returned HTTP
403. The verified working hub route is `nvidia/nvidia/nemotron-3-super-v3` at
`https://inference-api.nvidia.com/v1`, using Bearer authentication with
`INFERENCE_HUB_KEY` from the selected env file. The route name is recorded as
returned by the service; no equivalence of immutable model weights is asserted.

Store this non-secret configuration under the ignored private runs directory:

```json
{
  "model": "nvidia/nvidia/nemotron-3-super-v3",
  "endpoint": "https://inference-api.nvidia.com/v1",
  "credential_variable": "INFERENCE_HUB_KEY"
}
```

Pass it to `prepare` with `--provider-config PATH --case-count 4`. The selected
credential variable name is frozen in the manifest; only provider stages read
its value from `--env-file`. Offline freezing, comparison and candidate selection
need no credential. The default prepared suite has six examples; `--case-count 4`
selects the budget-limited development/final pairs used by the hub pilot and leaves
the other two examples explicitly unmeasured.

A fresh four-case run uses 24 council calls at three repetitions per arm, plus
bounded diagnosis. The pilot command caps a diagnosis attempt at 20 calls, so a
fresh 48-request budget leaves room for the complete four-case sequence without
connectivity probes. Shared budget reservations also support explicit continuation
across separately identified experiments; changing limits requires a recorded
operator decision, and failed reservations are never refunded.

Corrections earn credit only when delivered in the council response's existing
`guidance` field. The candidate encodes an explanation and correction object as a
JSON string in that field. Extra top-level model fields discarded by the council
cannot satisfy the verifier. Scored traces retain the independent check results,
correction rule, observed correction and recorded council-member interaction;
those model messages are not relabeled as a client conversation.

The [reviewed suite](suites/pilot-20261028/README.md) preserves the controlled
source bundle and sanitized definition used for the completed comparison. The
final family is exposed and must be retired before further informed tuning.
