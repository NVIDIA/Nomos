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
tasks with source finding/rationale/family/split/expected decision, two instruction
strings, provider model and endpoint, repetitions, budgets, version identities
and proof results from `prove(tasks)`. No source trace becomes a shell command.

Freeze stores an identity over the reviewed manifest and all Python implementation
files in the council, contracts and evaluation packages. Any subsequent code or
manifest change requires a new experiment. The manifest includes both splits;
keep it in the final evaluator's private workspace. Give the candidate designer
only `design.json`, which excludes held-out requests and labels. Filesystem access
separation is the operator's responsibility. No claim of enforced secrecy against
an operator who can read the final workspace is made.

This first implementation supports one candidate revision per experiment. Decide
its instruction-only change using development evidence, then freeze it with the
reviewed suite. If rejected, stop or start another reviewed experiment within an
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
operator-installed `/experiment/trial.json` containing `experiment` (the strict
manifest), `task_id`, and `arm`. Inject the provider secret through the environment
secret configuration. The fixed worker ignores instruction prose and writes
`/app/decision.json` for an independent verifier. Never put the verifier or oracle
in the agent image. The environment must be destroyed after every trial.

The local runner and Harbor worker share `adapter.trial`; the local runner owns
cross-trial accounting. Do not substitute Harbor worker invocations for the
manifest lifecycle or call-budget accounting. Native Harbor task-proof controls
are reproducible separately through the authoring example. Provider-backed Harbor
performance is pending a configured model environment.

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
