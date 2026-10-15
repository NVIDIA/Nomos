# Asynchronous agent assurance proof of concept

This service reads Nomos's signed ledger outside the authorization path, verifies
entries, and produces advisory findings. It uses actual deterministic NeMo
Compass code and exports drafts validated by Eval Author. It makes no model
calls and performs no automatic enforcement.

For the working live council integration, start with [LIVE.md](LIVE.md). It
describes the authenticated MCP reader, concurrent council writers, replicated
PostgreSQL deployment, certified executor evidence, and validated migration.
The instructions below also retain the isolated Kind fixture demonstration.

The Kind deployment contains an authenticated fixture source backed by Nomos's
`LedgerChain` and `PostgresLedgerStorage`, PostgreSQL, and an independent observer
with a SQLite PVC. The fixture source exposes the same ledger page/get tool
contract as the council. It does **not** deploy a council, OPA, or executor, and
fixture commands are never executed. This proves the observer integration and
recovery path; connecting a live council is a separate configuration step.

## Run locally

Prerequisites: Python 3.12 or 3.13, uv, Docker, Kind, kubectl, and an existing Kind
cluster (`kind` by default). The script uses `kind-<cluster>` explicitly and does
not change the current kubectl context. Only namespace `nomos-assurance-poc` is
modified. If needed, explicitly create a cluster with `kind create cluster`.

From this worktree:

```sh
uv sync --extra dev --extra assurance --extra openai
PYTHONPATH=. .venv/bin/python scripts/assurance_poc.py deploy --cluster kind
kubectl --context kind-kind -n nomos-assurance-poc port-forward \
  --address 127.0.0.1 svc/observer 9910:8080
```

Open <http://127.0.0.1:9910>. Username and password are in the ignored mode-0600
`.assurance-poc/access.env`. The script generates credentials and signing
material once, retains them on subsequent deployments, and never prints secrets.
Keep that directory together with the ledger PVC; replacing the signer breaks
trust in existing history. The reader, fixture operator, and dashboard have
separate credentials. Only the fixture source mounts the private signing key.

Upstream source is fetched directly from the public NVIDIA-NeMo repositories by
SSH into `.assurance-poc/upstream/`. `COMPASS_ROOT` and `EVAL_AUTHOR_ROOT` may point
to existing clean clones at these exact pins:

| Component | Commit | Used surface |
| --- | --- | --- |
| labs-nemo-compass | `11fefa0a6a2c20fa1eeda56447869b3a4fba88f8` | trace-ingest models and deterministic tool-issue evidence stream |
| labs-eval-author | `20f95a0a010d4905c13065678af51970404910c9` | audit-spec validator and schema |

The Docker build copies these surfaces and their licenses using named build
contexts. It does not install Compass's complete CLI/model stack. Dependency
versions for the service are exported from `uv.lock` to
`deploy/assurance/requirements.lock`. Rebuild with the deploy command after
changes; it reloads the image into Kind and restarts only these deployments.

## Exercise the deployment

### Trigger new reviews while watching the dashboard

Leave <http://127.0.0.1:9910> open. From this worktree, run:

```sh
PYTHONPATH=. .venv/bin/python scripts/assurance_live_tests.py
# Or select one case:
PYTHONPATH=. .venv/bin/python scripts/assurance_live_tests.py --scenario denial
```

Each invocation creates a new run ID and separate `live-<scenario>-<run-id>`
agents. The runner prints the agent ID, signed ledger sequences, and observed
findings after the async observer catches up. Select that agent in the dashboard
to inspect its evidence. The dashboard refreshes every five seconds.

| `--scenario` | Signed observations | Expected advisory findings |
| --- | --- | --- |
| `denial` | Same denied request three times without correction | 1 repeated unchanged denial |
| `retry-loop` | Three identical failed calls in each of three independent cases | 9 explicit failures + 3 retry patterns |
| `verification` | Verification failure with a simulated nonce replay reason | 1 verification failure; cause/intent not established |
| `benign` | Denied request, then corrected request approved | 0 findings |
| `all` (default) | All four cases, 15 records | 14 findings across 3 agents; benign agent stays clear |

These are synthetic observations through the live signed ledger, verification,
and analysis pipeline. They do not run commands, exercise a live council, or
establish that any agent is malicious. The benign case intentionally does not
appear in the findings-only agent filter; its processed sequences and zero-result
check are in the runner's report.

Reports are saved to `.assurance-poc/live-tests-<run-id>.json`. Add
`--run-id my-test-1` to use a memorable ID. Repeating the same scenario and run ID
reuses the original records and findings, including after a source restart; omit
it or choose a new ID to trigger fresh reviews. Entries persist in the demo ledger.
Do not run the corruption smoke test concurrently with a live test.

The operator-only `POST /fixtures/run` accepts just `scenario` and `run_id`
(1-40 lowercase letters, digits, hyphens; first character alphanumeric). It emits
at most nine fixed observations, rejects unknown/extra fields, and never accepts
arbitrary commands. The observer's read token cannot invoke this endpoint.
The CLI reads the operator credential locally and opens temporary loopback port
forwards; the dashboard and observer still have no fixture-write capability.

### Verify deployment recovery and export

```sh
PYTHONPATH=. .venv/bin/python scripts/assurance_smoke.py --cluster kind
```

This command opens temporary loopback port forwards, checks authentication, seeds
15 idempotent synthetic observations, waits for analysis, validates the exported
audit bundle, injects response corruption, verifies a stationary checkpoint,
restores normal responses, and restarts the observer to verify persistence.
It restores fault mode in a `finally` block and stops its temporary forwards.
The JSON report is written to `.assurance-poc/smoke-report.json`.

Expected behavioral findings before corruption: 14 total: 9 explicit tool
failures, 3 repeated identical failed-call patterns, 1 repeated unchanged denial,
and 1 verification failure. The three independent failure cases produce Compass
recurrence cards. The careful agent's corrected request produces no finding.
The injected corruption adds one retained integrity incident with no agent blame.

## What is verified

* Pages are bounded to 200 entries, 8 MiB, and a 15-second HTTP timeout.
* Ingestion starts at genesis, checks configured chain identity, contiguous
  sequence, previous hash, canonical content hash, and every Ed25519 signature
  against independently configured public keys. Unsigned or unknown signers fail.
* Every poll revalidates the remote checkpoint against the locally retained hash.
  Truncation, rewriting, or invalid data halts ingestion. Invalid responses never
  become trusted evidence. A retained integrity finding names the last trusted
  checkpoint, and the worker status reports degradation until recovery.
* Events and the ingestion cursor commit together. Analysis has a separate durable
  cursor, so a crash between ingestion and analysis does not silently lose work.
  Failed analysis prevents subsequent ingestion from outrunning pending work.
  Findings have stable IDs and insert idempotently. Run one observer replica with
  a `Recreate` strategy; this is not an HA queue implementation.
* Unsigned Nomos `metadata` is discarded. Agent attribution comes only from the
  signed payload and is explicitly a ledger writer's assertion, not an agent's
  independent signature or evidence of malicious intent.

A valid signature does not prove source completeness, honest timestamps, a
truthful writer, or the absence of competing histories. The PoC has one trust
anchor and no quorum/witness comparison. A source can hide a new tail while
serving an unchanged valid prefix. Historical entries before the checkpoint are
not re-fetched on each poll; the verified local copy is retained.

## Behavioral analysis

The local denial rule requires three denials for the same signed agent and exact
command/target fingerprint within 15 minutes of signed entry timestamps. It does
not equate an ordinary denial or a corrected request with bad behavior.
`verification_failed` is surfaced as a direct observation without interpreting
its cause. Single findings are retained even when Compass does not promote a
recurring problem.

Consolidated review records become `GUARDRAIL` spans. The decision alone is not
evidence that a tool executed. Council-validated certified execution summaries
inside signed review payloads become separate TOOL observations; see
[the live evidence contract](LIVE.md#what-compass-sees) for attribution,
deduplication, and correlation limits. Existing execution records usually lack enough run/call context
for reliable Compass tool analysis; those records are counted as unsupported.
For the demo, `execution_failed` records include a signed, proposed extension:

```json
{
  "assurance_schema": 1,
  "agent_id": "example-agent",
  "run_id": "run-1",
  "logical_case_id": "independent-case-1",
  "tool_name": "nomos.execute",
  "call_id": "call-1",
  "input": {"command": "/demo/unavailable"},
  "output": {"success": false, "error": "fixture service unavailable"},
  "synthetic": true
}
```

This extension is a PoC adapter contract, not an existing production receipt
schema. Only completed/failed observations are projected into tool spans; an
execution-start event is insufficient. Missing output stays missing; explicit
null stays null. Run IDs are scoped by chain and agent. Evidence links retain
chain, sequence, and content hash. Retry findings link each supporting call.

Analysis recomputes the last **2,000 verified entries** after ingestion advances.
Findings remain in SQLite; the API/export returns the latest **1,000 findings**.
This bounded window can miss long-distance patterns or runs crossing its edge.
The checkpoint is verified ingestion, not a guarantee of all behavioral coverage.
Retention, cross-chain ordering, backlog scheduling, and multi-replica leases
remain production work. SQLite retains raw signed payloads locally, so its PVC
must be treated as sensitive; it is not encrypted by this service.

## Eval Author handoff

`GET /api/export` returns a ZIP with `audit.md`, `ETHOS.md`, the validator's result,
a provenance manifest, findings, proposed replay work, and redacted canonical
trace records. The actual pinned Eval Author validator must return `valid: true`
before download succeeds. Source digests bind the draft to its evaluation brief.

The bundle omits raw commands, outputs, and error text. Redacted traces are for
provenance review and cannot be used to reproduce the original detectors. The
manifest explicitly says `coverage_status: unmeasured`: schema validation is not
Harbor replay, measured coverage, a signed executor receipt, or a passing eval.
Agent identifiers and evidence hashes remain visible to authenticated reviewers.
A reviewer must author independent cases and verifiers before using these drafts
as a regression evaluation suite.

## Connect a live council

Deploy the observer separately with `SOURCE_URL`, `SOURCE_TOKEN`, `COUNCIL_ID`,
`CHAIN_ID`, `TRUSTED_KEYS_FILE`, `STATE_PATH`, `DASHBOARD_USER`, and
`DASHBOARD_PASSWORD`; optionally set `POLL_SECONDS` (default 2).
The adapter calls:

* `POST /debug/tools/call/council_ledger_page` with council ID, cursor, limit.
* `POST /debug/tools/call/council_ledger_get` with council ID and sequence.

For the live council these routes belong to `assurance.council_source`, the
authenticated read-only bridge. The installed NAT frontend exposes ledger tools
through stateless MCP at `/mcp`; it does not provide these HTTP call routes.
Configure the bridge with `COUNCIL_SOURCE_URL`, `COUNCIL_ID`, and `SOURCE_TOKEN`,
then point the observer's `SOURCE_URL` at the bridge. See
[LIVE.md](LIVE.md) and `deploy/assurance/live-compose.yaml` for the deployment.

A direct object or NAT `result` wrapper containing an object/JSON string is
accepted. Put real deployments behind authenticated TLS transport; the Kind
example uses cluster-internal HTTP and a loopback-only browser forward.
Configure trusted public keys independently of the data source. The local state
is bound to a chain ID and cannot be reused for another chain. A restart with a
new source claiming the same chain still has to match the saved checkpoint.
No signing key or fixture-operator token is needed by the observer.

## Tests and cleanup

```sh
PYTHONPATH=.:.assurance-poc/upstream/compass/src:.assurance-poc/upstream/compass/packages/trace-ingest/src \
  .venv/bin/pytest tests/assurance -q
# Add the same PYTHONPATH for the full suite; openai is an existing suite dependency.
.venv/bin/python scripts/validate_council_routing.py --mode public
```

The independent service imports Nomos primitives inward; Nomos core does not
import assurance or either upstream repository. Authentication, integrity failure,
restart recovery, findings, redaction, and the real upstream contracts are tested.

To stop the local demo without deleting data, scale `observer` and `ledger-source`
to zero and scale `statefulset/ledger-db` to zero in `nomos-assurance-poc`. Stop
port-forward processes separately. Deleting the namespace removes this demo's
PVCs and data; do that only when the evidence is no longer needed.

## Reading the dashboard

The overview separates current monitor health, verified ledger records, analysis
progress, and agents with findings. Record counts include genesis at sequence
zero. Analysis progress is relative to the observer's verified checkpoint, not a
claim that the source has disclosed every possible event. Old checks and failed
refreshes are visibly marked instead of retaining a healthy status.

The review queue groups findings by agent, behavior, and whether the records are
synthetic. Each group explains the trigger, its significance, and a suggested
review step. Expand a group to see its deduplicated ledger references; record
fingerprints and detector IDs are available on demand. A record can contribute
to several groups (for example a failed call and a retry pattern), so finding
counts should not be read as unique actions or incidents.

Ledger verification incidents have their own history panel. They are not counted
as agent-behavior findings and are not attributed to an agent. A healthy current
poll does not remove a retained incident. The coverage panel identifies observed
reviews and tool calls, missing execution context, and analysis limitations.
The export action labels the bundle as a draft that still needs replay tests.

The presentation model has focused tests for grouping, overlapping evidence,
synthetic data, stale status, and filtering:

```sh
node --test tests/assurance/dashboard_model.test.mjs
```
