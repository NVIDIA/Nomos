# Asynchronous agent assurance

The assurance service reads and verifies Nomos's signed ledger, then periodically
runs selected NeMo Compass components and Nomos rules to produce advisory
findings. It operates outside authorization: findings do not block requests,
reverse council decisions, revoke permissions, or establish malicious intent.

Assurance is **disabled by default**. ETHOS inference and validated audit exports
have separate opt-in settings. With assurance disabled, no observer workers or
assurance API/dashboard routes start. Nomos core requires no Compass installation.

- [Compose and Helm deployment](DEPLOYMENT.md)
- [ETHOS document ownership, versions, and agent mappings](ethos/README.md)
- [Evaluation scenarios and verification](EVALUATION.md)
- [Implementation plan](PLAN.md)

## Supported analysis

| Analysis | Inputs and behavior | Inference |
| --- | --- | --- |
| Compass tool issues | Recorded tool calls and outcomes; rule eligibility depends on available schemas, correlation, and results | None |
| Compass ETHOS divergence | Versioned operator-managed requirements selected for authenticated agent identities and event times | Explicitly enabled approved endpoint |
| Nomos rules | Repeated unchanged denials and recorded verification failures | None |

Only these two Compass signals are selected. Sentiment, grader failures,
anomaly analysis, full insight compilation, and automatic code review do not run.
The service calls Compass Python components directly, without the full CLI.

Requested, authorized, and executed actions remain distinct. Consolidated council
reviews provide intent and review context; execution outcomes require recorded
execution evidence. Repeated denials are a Nomos rule, not failed tool calls.
Certified receipts are deduplicated and correlated by their recorded identities;
unknown run/case correlation remains a coverage limitation. Signed synthetic
observations are labeled separately from ordinary recorded activity.

ETHOS requirements declare the evidence they need. A requirement about requested
targets can use request evidence; a claim about completed behavior needs execution
evidence; a conversational obligation requires complete conversation context.
Unsupported requirements are reported as insufficient evidence rather than passed.
An unmapped agent remains eligible for tool checks and is reported as unconfigured
for ETHOS. The service does not infer roles or author its own ETHOS documents.

## Scheduling and state

Ledger polling defaults to **two seconds** (`POLL_SECONDS`); analysis defaults to
**five minutes** (`ANALYSIS_SECONDS`). Tool and ETHOS streams have independent
workers, progress, retries, and timeouts, so an inference failure does not stop
successful tool checks. Unchanged evidence/configuration does not trigger another
inference run. Changes to the ETHOS registry or model configuration create a new
analysis scope without overwriting historical runs.

Each run captures an immutable verified input window, checkpoint, detector and
configuration identity, coverage, and applicable ETHOS registry snapshots.
Pending evidence is processed in bounded windows with overlap for local patterns;
findings have stable IDs. Failed runs retry within configured limits and remain
visible when retries are exhausted. Operators can retry a recorded run explicitly.

SQLite stores verified payloads, findings, run inputs, and ETHOS snapshots on a
persistent volume. Run exactly **one observer** per state database. Ingestion
uses bounded backpressure if analysis falls behind. Raw state is sensitive;
protect the volume and preserve it across restarts. This is not a multi-replica
queue or a cross-chain analysis service.

Verification checks chain identity, sequence continuity, previous hashes, content
hashes, and trusted Ed25519 signatures. Source truncation, checkpoint rewriting,
and invalid pages do not become evidence. Integrity incidents retain the last
trusted checkpoint without blaming an agent. A signature establishes the writer's
record, not honest content, a complete disclosed history, or independent agent
identity. Unsigned entry metadata is excluded from analysis.

## Protected API and dashboard

The browser dashboard groups related findings by agent and behavior, separates
ledger incidents, and shows coverage and independent stream freshness. Run history
is paginated. Failed, skipped, unconfigured, or insufficient-evidence analysis is
visible separately from an empty findings queue. ETHOS candidates show the
requirement, document version/hash, and supported explanation for human review.

HTTP Basic credentials permit human review and operator actions. A separate
`API_READ_TOKEN` permits read-only machine API access with a Bearer header.
Health probes (`/healthz`) are public; they report process availability, not analysis
freshness. Deploy behind authenticated TLS when access extends beyond a trusted
local network. See [deployment configuration](DEPLOYMENT.md) for credential,
network, persistence, and inference settings.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/status` | Verified checkpoint, worker health, stream progress/backlog, and coverage |
| `GET /api/findings` | Findings with pagination and agent, stream, run, kind, disposition, and time filters |
| `GET /api/findings/{id}` | One finding and its evidence/provenance |
| `GET /api/runs` | Paginated run history with stream, status, agent, and time filters |
| `GET /api/runs/{id}` | Run metadata and coverage, without raw inputs/document contents |
| `POST /api/runs/{id}/retry` | Operator Basic authentication and `X-Assurance-Operator: retry` header required |
| `GET /api/export` | Optional validated audit ZIP; unavailable when export is disabled |

Retry requests must also have `Sec-Fetch-Site: same-origin` when that browser
header is present. Cross-site and same-site requests are rejected; machine
clients may omit `Sec-Fetch-Site`. The machine read token cannot retry runs.

List responses include `total`, `limit`, `offset`, and `next_offset`; findings use
the `findings` collection key and runs use `items`. Limits are 1–1,000. Time filters
(`since`, `until`) require timezone-aware timestamps. Retained findings may overlap
and should not be counted as unique incidents or actions.

## Dependencies and local development

Use Python **3.12 or 3.13** for assurance. The `assurance-compass` extra installs the
reviewed Compass distribution at Git commit
`518ad41d583e259205fb89623cb035ae3904f3f8`, including `trace_ingest` and its inference
dependencies. The integration does not require an external Compass deployment.
Nomos's ordinary dependencies remain separate from this optional runtime.

```sh
uv sync --extra all --extra assurance-compass
.venv/bin/pytest tests/assurance -q
node --test tests/assurance/dashboard_model.test.mjs
```

The Dockerfile builds that pinned Compass distribution from public HTTPS Git;
`uv.lock` and `deploy/assurance/requirements.lock` lock its runtime dependencies.
The assurance extra pins LiteLLM to 1.105.1: 1.106.0 incorrectly loads optional
MCP 2.x components for ordinary function-tool inference. The runtime regression
test sends a real tool-bearing completion to a local HTTP fixture, exercising
the import path without an external model or credentials. Keep this check when
updating the pin, and validate ETHOS against an approved endpoint before rollout.
The ETHOS adapter removes NOOA's optional `prompt_cache_key` routing hint because
some OpenAI-compatible endpoints, including NVIDIA Nemotron Ultra, reject it.
Function tools and other request validation remain enabled; the HTTP regression
test also exercises this adapter path.
ETHOS run provenance includes the installed LiteLLM and NOOA versions. Changing
either creates a new analysis scope, so dependency repairs reprocess evidence
without reusing failed runs or cached results from the previous runtime.
The installed wheel includes upstream licenses and notices. No SSH credentials
or source checkout mounts are needed in the running container. To regenerate the
container requirements after an intentional dependency update:

```sh
uv export --extra assurance-compass --no-dev --no-hashes --no-emit-project \
  -o deploy/assurance/requirements.lock
```

## Optional Eval Author export

Eval Author is a separate offline authoring/evaluation project. Monitoring and
ETHOS checking do not require it. The default image excludes its tooling.
Developers may use its authoring tools offline to prepare reviewed documents.

The optional export image adds only the pinned audit validation tooling, not
ETHOS authoring skills. Build it explicitly:

```sh
.venv/bin/python scripts/assurance_poc.py bootstrap --with-export
docker build -f deploy/assurance/Dockerfile --target assurance-export \
  --build-context evalauthor=.assurance-poc/upstream/eval-author \
  -t nomos-assurance:export-local .
```

The helper checks clean source at Eval Author commit
`20f95a0a010d4905c13065678af51970404910c9`. `EVAL_AUTHOR_ROOT` can identify an existing
clean checkout at that revision. Deploy this image and explicitly enable
`ASSURANCE_EXPORT_ENABLED=true`; enabling exports with no validator fails startup.

Exports contain findings, run metadata, evidence references, a draft audit, and
redacted trace identifiers. They include the exact immutable ETHOS documents and
registry provenance saved with exported runs, not a newly generated generic brief.
Configured historical versions are provenance; run coverage identifies which
requirements were evaluated. If no document was configured, export does not invent
one. Exports include up to 1,000 recent runs and a 2,000-record evidence window,
with those bounds recorded in the manifest.

The real pinned validator must return `valid: true` before download. That verifies
audit format and source hashes, not detector accuracy or evaluation coverage.
Redacted traces cannot reproduce the original analysis; reviewers must author
independent cases and verifiers. Validation results and manifests explicitly mark
the handoff as a draft with unmeasured coverage.

## Isolated fixture deployment

The retained `scripts/assurance_poc.py deploy --cluster <name>` helper builds the
image and installs an explicitly enabled observer, signed fixture source, and
fixture database into `nomos-assurance-poc` in an existing Kind cluster. It does
not deploy a real council or execute fixture commands. Add `--with-export` to
include and enable audit validation. Credentials and signer material persist in
the ignored, private `.assurance-poc` directory; rebuilds preserve existing trust.
Use [the standard Compose/Helm integration](DEPLOYMENT.md) for a real council.

## Private evaluation authoring bundles

`GET /api/export` keeps its existing provenance-only format and read credentials.
A separate `ASSURANCE_EVALUATION_EXPORT_ENABLED=true` enables richer content for
operators. It is disabled by default and does not enable either ETHOS inference
or the legacy audit export. The dashboard supports individual/group selection,
an explicit council/client subject, a context preview, and a frozen download.

Both `POST /api/evaluation-export/preview` and `POST /api/evaluation-export` require
operator Basic authentication and `X-Assurance-Operator: evaluation-export`.
Browsers must be same-origin; non-browser clients may omit `Sec-Fetch-Site`.
The machine read token cannot use either endpoint. Preview accepts only
`{"finding_ids":["<id>"],"subject":"council-decision"}` (or `client-agent`). Download
accepts only `{"capture_id":"<preview capture_id>"}`. A capture is immutable even
if the ledger advances; expired/evicted captures return 409 and need a new preview.

The versioned `nomos.evaluation-authoring.v1` ZIP includes selected findings,
originating run checkpoints/config digests, transformed supporting records,
reference resolution, applicable historical ETHOS documents, completeness,
redactions, validation, instructions, and an inventory of exported-byte hashes.
Source hashes identify historical originals, not the transformed exported bytes.
Signatures and reusable capabilities are omitted. Related records join only on
explicit intent/token or signed agent/run/case identities. An ETHOS context run
must share an exact selected source reference. Unrelated recent events are not
substituted for missing historical evidence. Native execution identity is resolved through the same explicit signed
intent/token links used by analysis, at the execution record's own timestamp.
An ETHOS finding must retain its exact cited contract for its primary evidence;
a contract covering only a related earlier request is insufficient.
Historical ETHOS mapping, version,
effective dates, requirements, and applicable references accompany each document.

Council-only evidence uses Eval Author's repository-based first-eval workflow;
no council explanation is converted into client dialogue. A narrow client path
emits ATIF-v1.0 only for explicit signed observations with consistent complete
pre-tool conversation, run/case IDs, actual arguments, call/result linkage and
results. Its source asserts `assurance_context.messages_position="before_tools"`
as well as `schema_version=1` and `conversation_complete=true`. This optional
capture field records where supplied dialogue belongs; ordinary council reviews
do not supply it. Ambiguous, nested, partial, or conflicting captures remain
unsupported for trace derivation. No general conversation collector is added.
Each advertised trace passes the pinned Eval Author `init`/`prepare` commands;
the exact validation-script hashes are checked before execution. Missing client
agent versions use the explicit `unrecorded` marker and remain a missing input.
The offline pilot also checks this serializer with Harbor's native ATIF reader.

Optional recorded tools and environment descriptions appear only when present.
Repeated certified receipt summaries share one identity, with original source
references and conflicting variants explicit. A summary hash cannot recover
missing output. The exporter never reads source-provided paths or URLs.

Limits are 20 selected findings, 2,000 source records, 8 MiB per capture, 20 bounded
ETHOS context-run candidates, and four unique validated client traces. Omissions
and unavailable inputs are explicit per candidate. Four ZIPs are cached in process
memory, expire after ten minutes, and are purged on export access or process exit;
no capture is persisted on the server. Restart requires a new preview. Only
capture IDs and counts enter activity logs. Do not enable request-body logging.

The `private-operator-v1` profile uses field allowlists, removes credential/capability
fields, masks common credential forms and configured exact sensitive values,
and bounds free text. Set `ASSURANCE_EXPORT_REDACT_VALUES` to a private JSON array
of additional exact values. Commands, outputs, explanations, identifiers and ETHOS
all undergo the same content policy. Loss triggers semantic review in completeness.
This is a private authoring artifact: automated matching cannot prove arbitrary
text safe to publish. Review it before sharing, and delete local downloads when
the authoring work finishes according to your organization's retention policy.

Trace compatibility, audit schema validation, task proof, and actual agent
performance are distinct. See [the reproducible offline pilot](EVALUATION.md#evaluation-authoring-pilot)
for the tested toolchain, external prerequisites, and positive/negative controls.

The follow-up [offline optimization workflow](../../evals/nomos/README.md) adds
private intake, frozen council experiments, bounded trial accounting, comparison
gates and scored diagnosis. See [current implementation evidence and pending
pilot acceptance](OPTIMIZATION_LOOP.md) before making performance claims.
