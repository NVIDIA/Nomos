# Asynchronous assurance implementation

Local branch: `feat/async-assurance`. Integration target: `upstream/develop`.
Changes remain local; no feature push or pull request has been published.

The implementation follows [PLAN.md](PLAN.md): opt-in monitoring, independent
Compass tool-issues and ETHOS-divergence streams, durable verified evidence and
run history, authenticated results and dashboard, static versioned ETHOS
configuration, Compose and Helm deployment, and reproducible evaluation.

## Verification evidence

- Built the default assurance image and the optional export image; runtime
  imports work with networking disabled. Eval Author is absent from the default
  image and available only in the optional export target.
- Ran the assurance Python suite, dashboard model tests, labeled deterministic
  evaluation, formatting, lint, type checks, and Helm rendering/lint checks.
- Started the real Compose overlay against a signed synthetic MCP ledger.
  Verified checkpoint advancement, authenticated results, integrity-failure
  checkpoint preservation and recovery, and unchanged state after restart.
- Installed the full Helm chart in a temporary local Kind namespace, with its
  actual assurance reader, observer, and PVC. A signed synthetic MCP proxy
  replaced the council. Verified authentication, processing, and identical
  checkpoint/findings after replacing the observer pod. Removed the temporary
  release and namespace afterward.
- Local smoke-test receipts are in `/private/tmp/nomos-assurance-smoke/`.
  The fixtures check deployment plumbing; they do not establish production
  council connectivity or representative finding accuracy.

## Remaining operator validation

No approved inference endpoint or production agent purpose contract was supplied.
Real ETHOS inference and representative owner-labeled quality evaluation require
that configuration. Unit tests exercise the adapter with controlled responses;
`assurance_evaluate.py --ethos` provides the opt-in real-provider check.

Kind's default CNI does not enforce NetworkPolicy. Policies are rendered and
installed, but enforcement needs a cluster with a supporting network plugin.
Production council connectivity and deployment identity/TLS controls also need
validation in the intended environment.

The broad core suite initially hit macOS Bash 3.2 syntax limitations and denied
local socket binds. All 269 initially failing/erroring cases passed with modern
Bash and permitted local sockets. The full CI-style core run then passed 1,440
tests, skipped 135 service-dependent/optional cases, and failed two registration
checks because its reduced PATH omitted `/sbin/sha256sum`. Both checks passed on
a targeted rerun with the complete tool PATH. No core source changes were needed.
Live Redis, PostgreSQL, and OPA checks remain pending their test prerequisites.
Independent review-loop findings, repairs, and verification receipts are saved
outside the repository under `/private/tmp/nomos-assurance-review/`.

## Review repairs

The first independent review identified automatic NOOA telemetry initialization,
orphan-result provenance, conversation grounding, and evaluation deadlines that
were shorter than the analysis cadence. These were corrected with focused
regressions. Detector/evidence versions were advanced to reanalyze retained
evidence after the projection fixes. The follow-up review checks those repairs
and their direct effects. Verification uses explicit Docker Compose discovery
under its temporary HOME, keeping user Docker credentials out of the checks.

The isolated type check also exposed four Redis annotation errors reproduced
unchanged on `upstream/develop`. Its verification environment now includes the
Redis and YAML stubs already prescribed by `.pre-commit-config.yaml`; no core
source or dependency-lock change was needed. The first review receipt is
preserved. A fresh session carries its four findings into a focused re-review
with corrected check prerequisites and all original verification gates retained.

The focused reviewer confirmed the four original repairs and identified nullable
nested tool-call metadata in the grounding repair. Normalizing canonical null
metadata now preserves the supported input; its signed-ingestion regression
verifies tool and ETHOS publication/progress plus exported ledger references.
