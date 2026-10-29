<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Add asynchronous agent assurance with NeMo Compass

## Goal

Add an assurance service to periodically analyze verified Nomos ledger history
using Compass tool checks and ETHOS divergence analysis. Publish advisory findings
through a protected API and dashboard for human review.

## Proposed behavior

The service periodically reads and verifies ledger records for approved and
rejected requests, together with available execution outcomes. It correlates
related events to identify patterns across an agent's activity.

- **Tool checks:** detect tool failures, unproductive retries, and repeated
  unchanged requests after denial.
- **ETHOS checks:** compare recorded behavior with the agent's configured ETHOS
  document to identify possible deviations from its purpose and constraints.

Publish findings and analysis status through an authenticated API and dashboard,
with supporting evidence and explicit coverage limitations. Analysis runs
independently of Nomos authorization and produces advisory results for human
review.

Assurance is disabled by default and requires explicit opt-in in Compose and
Helm deployments. ETHOS inference is separately optional and requires explicit
configuration of an approved inference endpoint.

## Scope

1. **Evidence and ETHOS contracts:** map agents to versioned ETHOS documents,
   correlate related ledger events, and identify additional context needed for
   purpose checks.
2. **Reliable periodic analysis:** support configurable analysis intervals and
   resume after interruptions without losing work or duplicating findings.
3. **Optional ETHOS analysis:** support approved inference with usage limits and
   evidence-backed findings. Tool checks remain available when ETHOS analysis
   is disabled or unavailable.
4. **Protected results:** extend the API and dashboard with paginated findings,
   run status, filters, freshness, and coverage information.
5. **Deployment:** support opt-in enablement through Compose and Helm configuration,
   with setup documentation and examples.
6. **Validation:** provide a reproducible local deployment and a labeled
   evaluation covering tool behavior and purpose divergence.

## Acceptance criteria

- [ ] Assurance is disabled by default in Compose and Helm; enabling it requires
  explicit configuration. ETHOS inference requires a separate opt-in.
- [ ] Tool and ETHOS checks analyze recorded activity outside the authorization
  path and produce findings linked to supporting ledger evidence.
- [ ] ETHOS findings identify the applicable document version and requirement;
  missing context is reported as insufficient evidence.
- [ ] Analysis resumes after interruptions without losing work or duplicating
  findings. ETHOS inference failures do not block tool results.
- [ ] Authenticated clients can retrieve findings and distinguish successful,
  empty, skipped, partial, and failed analysis runs.
- [ ] Tests cover compliant and violating behavior, repeated denials, bounded
  recovery, missing context, integrity failures, and inference outages.
- [ ] A labeled evaluation measures finding quality, evidence validity, latency,
  and inference usage.
- [ ] Packaging and setup documentation support a reproducible local deployment;
  the implementation passes applicable project checks.

## References

- [NeMo Compass](https://github.com/NVIDIA-NeMo/nemo-compass)
- [Eval Author ETHOS tooling](https://github.com/NVIDIA-NeMo/eval-author/tree/main/team-skills/ethos)
