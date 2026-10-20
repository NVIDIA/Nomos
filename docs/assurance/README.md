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
| `POST /api/runs/{id}/retry` | Operator-only retry of an existing run |
| `GET /api/export` | Optional validated audit ZIP; unavailable when export is disabled |

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
