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
