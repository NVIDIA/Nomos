# Assurance verification summary

Status as of October 21, 2026, for [PR #130](https://github.com/NVIDIA/Nomos/pull/130),
which targets `develop` and supersedes the original assurance PoC in #111.

The implementation follows [PLAN.md](PLAN.md): opt-in monitoring, independent
Compass tool-issues and ETHOS-divergence streams, durable verified evidence and
run history, authenticated results and dashboard, static versioned ETHOS
configuration, Compose and Helm deployment, and reproducible evaluation.

## Recorded validation

Before the CodeRabbit follow-up changes, the assurance suite passed 204 tests,
including runtime regressions with pinned LiteLLM 1.105.1 and audit export
integration with pinned Eval Author `20f95a0`. The dashboard model passed 12 tests.
The real Eval Author validator accepted the audit archive and its sensitive
fixture payload was excluded. PR #130's enabled CI jobs passed on the submitted
revision `f944871`.

Earlier local deployment exercises covered:

- Building the default assurance image and optional export image, with runtime
  imports working without networking. Eval Author was absent from the default
  image and present only in the export target.
- Running the Compose overlay against a signed synthetic MCP ledger, checking
  authenticated results, checkpoint advancement, integrity-failure recovery,
  and retained state after restart.
- Installing the Helm chart in a temporary Kind namespace with the assurance
  reader, observer, and PVC. A signed synthetic MCP proxy replaced the council;
  checkpoint and findings survived replacing the observer pod.

These exercises validate fixture behavior and deployment plumbing. They do not
establish production council connectivity or representative detection accuracy.
Local checks for subsequent changes must be assessed against their own revision;
the earlier results above do not validate later edits.

## Remaining rollout validation

Real-provider ETHOS quality and representative owner-labeled evaluation require
an approved inference endpoint and production agent purpose contracts. Controlled
runtime tests do not establish production detection accuracy.
`assurance_evaluate.py --ethos` provides the opt-in real-provider check.

Kind's default CNI does not enforce NetworkPolicy. Policies were rendered and
installed, but enforcement requires a cluster with a supporting network plugin.
Production council connectivity and deployment identity/TLS controls also need
validation in the intended environment. Live Redis, PostgreSQL, and OPA checks
require their service prerequisites.
