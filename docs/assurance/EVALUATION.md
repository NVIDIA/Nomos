# Evaluate the assurance adapters

Run the reproducible, labeled fixture suite from the repository root:

```sh
uv sync --python 3.13 --frozen --extra all --extra assurance-compass
uv run python scripts/assurance_evaluate.py > /tmp/assurance-evaluation.json
```

The default invokes the real pinned Compass tool-issues adapter plus Nomos's
unchanged-denial rule. It makes no inference requests and executes no fixture
commands. Cases cover unchanged denials versus corrected requests, repeated
failed calls versus bounded recovery, and repeated citations of one receipt.
Labels and rationales are visible in the script and JSON output. Each label
addresses the named detector: a failed call can legitimately produce an explicit
failure observation while the bounded-recovery case remains negative for a retry
loop. Receipt duplication must yield one failure observation rather than a retry.

Exit status is 0 when the evaluated fixture contracts pass, 1 for mismatches or
inference failures, and 2 for configuration/execution errors. Output records the Compass revision, case labels, case predictions, findings
counts, supporting-reference validity, and observed wall time. Precision and
recall use only cases with both a Boolean label and a non-abstained prediction.
Abstentions, expected abstentions, inference failures, and incorrectly evaluated
missing-evidence cases are counted separately. Invalid references are checked
against the supplied chain, sequence, and hash.

These five small synthetic cases check integration behavior, not representative
agent accuracy. They bypass ledger ingestion and do not establish signature
verification, production identity attribution, or realistic incident prevalence.
The human labels express the authored example expectations; obtain independent
owner/reviewer labels on representative held-out traces before measuring real
operational finding quality or tuning detector thresholds.

## Optional real ETHOS evaluation

The same runner can invoke the actual Compass ETHOS signal through an explicitly
configured approved inference endpoint. This sends only synthetic fixture
content and example requirements. Review
[the example ETHOS document](ethos/DEPLOYMENT_EXAMPLE.md) and
[its mapping](ethos/mapping.example.json) first. The example remains limited to
local evaluation; it is not assigned to production identities.

Supply settings through a private environment or approved secret injection:

- `ASSURANCE_MODEL`: explicit `openai/<model>` or `nvidia_nim/<model>` identifier.
- `ASSURANCE_API_BASE`: approved HTTP(S) inference base URL.
- `ASSURANCE_API_KEY`: endpoint credential.
- Optional service-compatible limits: `ASSURANCE_INFERENCE_TIMEOUT`,
  `ASSURANCE_MAX_TOKENS`, and `ASSURANCE_MAX_MODEL_CALLS`.

```sh
uv run python scripts/assurance_evaluate.py --ethos > /tmp/assurance-ethos-evaluation.json
```

No worker, ledger access, or deployment is started. The runner uses the service's
inference-setting defaults, with the same bounded adapter. Call budgets apply
per fixture case. It evaluates compliant/violating staging and production
requests, successful executions, approval without execution, and missing
conversation context. Execution and conversation requirements with missing
inputs should abstain. Other eligible requirements may still produce model
requests in that fixture; usage counts cover all requests actually attempted.

Use `--ethos-mapping /absolute/path/ethos.json` for a reviewed copy of the example
mapping. It must retain the example agent and requirement IDs and corresponding
meaning, because the cases' labels refer to that contract. Changing the contract
requires reviewing the case labels as well.

The JSON reports actual attempted inference requests, model identifier, immutable
registry digest, failures, abstentions, and case-level metrics. It does not
estimate token usage or cost: the current adapter does not return provider token
accounting, so those values are explicitly unavailable. Credentials, raw prompts,
model explanations, and endpoint exception bodies are excluded from the report.
A failed or timed-out model call is an abstention, never a successful negative.
No real ETHOS accuracy result is claimed until this opt-in command is run against
a working approved endpoint and its results are reviewed.

## Automated checks

The dedicated CI assurance job measures assurance coverage with a 79% floor,
installs the optional pinned Compass distribution
on Python 3.13, runs `tests/assurance`, executes the dashboard model tests, runs
the deterministic evaluation, and lints/renders disabled, deterministic-only,
and ETHOS-enabled Helm examples. Deployment tests validate Compose configuration
without starting containers. The existing core unit job excludes the assurance
suite; core Nomos does not require Compass. CI never requests external inference.

For local checks:

```sh
uv run pytest tests/assurance
node --test tests/assurance/dashboard_model.test.mjs
```

Use separate deployment smoke tests for authentication, council connectivity,
state persistence, and approved inference connectivity; see
[deployment validation](DEPLOYMENT.md#validate-a-deployment).

## Evaluation authoring pilot

The authoring bundle adds a tested offline path, separate from the earlier audit
schema check. The pinned [Eval Author revision](https://github.com/NVIDIA-NeMo/eval-author/tree/20f95a0a010d4905c13065678af51970404910c9)
supports a repository-based `eval-author-first-eval` workflow when no complete
client trace exists. Council-only captures use that route. Its trace preparation
workflow accepts the exporter's narrow ATIF-v1.0 client projection; the real
`init` and `prepare` commands run in integration tests. A prepared trace is not
a proven task or ground truth.

The controlled council pilot uses Python 3.13 and **Harbor 0.23.0** in a separate
environment. It runs disposable `CouncilService.propose_intent` calls with a
local signing identity and deterministic test member: three production denials
and an accepted staging correction. Each request reaches member review; OPA is
disabled in this fixture. These decisions are test infrastructure and establish
no production council prompt quality. Proposed commands never execute.

Assurance verifies the signed ledger, runs its ordinary detector, selects the
resulting repeated-denial finding, and exports it through the operator preview
and download APIs. A test secret in the source context must be absent from every
ZIP member. The client-subject preview of this same council-only capture must
report `unsupported_for_trace_derivation` and contain no fabricated instruction
or result. The corrected request is a separate intent and is intentionally
absent from the selected finding's export.

The reviewed [Harbor task template](../../examples/assurance-evaluation/council-scope/README.md)
was authored using the pinned first-eval/task-validation guidance. The explicit
pilot scope is one offline council classification case, with three recorded
production proposals plus two **designed variations**: staging and missing scope.
Those variations are author-supplied fixtures, not recovered conversation or
historical execution. Task preparation reads only the downloaded bundle and the
reviewed template; private ledger/key files are not task inputs. No actual client
conversation or production agent is available in this pilot.

The environment contains Python, the downloaded evidence, and request fixtures.
Its build context excludes reference solutions and grading files. The verifier
uses its own request copy and checks every decision, requiring a complete JSON
answer. Expected behavior follows the exported staging-only fixture contract
and the instruction's explicit missing-scope rule, not recorded council votes.
There is no live side effect. Alternate JSON order/whitespace is accepted.

Reproduce in a private working directory from a clean Nomos checkout:

```bash
# Nomos's normal development environment also needs the pinned assurance extra.
uv sync --python 3.13 --frozen --extra all --extra assurance-compass

git clone https://github.com/NVIDIA-NeMo/eval-author.git /tmp/nomos-eval-author
git -C /tmp/nomos-eval-author checkout 20f95a0a010d4905c13065678af51970404910c9
uv venv --python 3.13 /tmp/nomos-harbor
uv pip install --python /tmp/nomos-harbor/bin/python 'harbor==0.23.0'

# Requires a working Docker daemon and its Compose/buildx plugins.
EVAL_AUTHOR_ROOT=/tmp/nomos-eval-author .venv/bin/python \
  scripts/assurance_authoring_example.py \
  --out /tmp/nomos-authoring-pilot \
  --harbor-python /tmp/nomos-harbor/bin/python
```

Choose a new output directory for each attempt; evidence receipts are never
overwritten. Omit `--harbor-python` to prepare the capture and task without
executing controls. Harbor's native Docker runtime uses its default public
network policy; the task and controls need no live model or external service.
On Docker Desktop the runner creates a writable private Docker configuration
with the native plugin directory, without copying registry credentials.

The script records immutable task inputs with the pinned Eval Author
`eval-author-task-create/scripts/task_evidence.py prepare`, then wraps four
separate native `harbor trial start` invocations with its `run` command:

| Control | Expected reward | Meaning |
| --- | --- | --- |
| `oracle` | 1 | Known correct scope decisions |
| `nop` | 0 | No answer earns no completion credit |
| `controls:IncorrectScope` | 0 | Approving every request violates scope |
| `controls:Alternative` | 1 | Equivalent valid JSON remains accepted |

The same offline check separately passes a clearly labeled synthetic client
format fixture through Harbor's native ATIF reader. This checks serialization,
including an explicit marker for an unrecorded agent version; it does not create
a real client trajectory or prove a client task.

Each receipt must report `status: passed`, each native trial must have no
exception, and the measured reward must match its control. `capture-receipt.json`
records the source verification, member-review counts, selection, capture/ZIP
hashes, secret check and incomplete-client result. `.eval-author/task-measurements/`
retains the task revision, contract, commands and control receipts;
`.eval-author/jobs/` retains native results. `proof-receipt.json` summarizes the
pinned versions and outcomes. Preserve these private receipts with the downloaded
ZIP; a rerun generates fresh signing IDs and timestamps, so its hashes differ.

This proves one downloaded-evidence-to-task/verifier path. It does not run the
real council/client agent in Harbor, close an audit coverage gap, or demonstrate
optimization. A recipient still supplies their actual agent/council repository,
runtime/provider configuration, applicable contract when absent, tool schemas,
and unrecorded world state. Use the bundle README to select a repository-based
or supported trace workflow, review the candidate's semantics/privacy, prove
its controls, then run the actual agent before comparing a baseline and candidate.
