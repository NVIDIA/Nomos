<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Jev (member zero)

Jev is a fast classifier that runs once per intent, after OPA and before the council. It answers a versioned question
catalog about the request in a few hundred milliseconds. It never votes. Depending on the level you enable, its
answers inform the council, set the chairman's risk level, or authorize the intent without convening the council.

```
agent ──► OPA ──► Jev ──► fast path? ──yes──► writ (no council)
                   │          │
                   │          no
                   ▼          ▼
            ledger record   council (members see every Jev answer) ──► writ or denial
```

A Jev failure never blocks. The intent goes to the council without a Jev record, and a Jev chairman falls back to
unanimous / high.

## Levels

The levels stack. Each needs every level below it. The council refuses to start with a level enabled and one
below it disabled, so a misconfiguration is an error rather than a silent no-op.

| Level | Setting (`jev.` in `council.yaml`) | Env / Helm value | What it adds |
|---|---|---|---|
| 1 | `enabled` | `COUNCIL_JEV_ENABLED` / `council.jev.enabled` | **Prefilter.** Attestations are attached to the intent, shown in full to every council member and recorded in the ledger. With `chairman.provider: jev`, Jev also sets the risk level and threshold. Every intent still goes to the council. |
| 2 | `fast_path.enabled` | `COUNCIL_JEV_FAST_PATH` / `council.jev.fastPath` | **Read-only fast path.** A read-only intent gets a writ without the council when Jev's read-only gate, risk level and adverse read questions all pass. |
| 3 | `mutation_fast_path.enabled` | `COUNCIL_JEV_MUTATION_FAST_PATH` / `council.jev.mutationFastPath` | **Low-risk mutation fast path.** A state-changing intent gets a writ without the council when every listed question is answered and under its limit: local, reversible, narrowly scoped work. |
| 4–6 | `moderate_risk_fast_path`, `high_risk_fast_path`, `critical_risk_fast_path` | none | **Reserved.** The settings exist so the level order is fixed now. No router exists for them, and `enabled: true` is rejected. |

Both fast paths also require all of these:

- OPA ran and allowed the intent.
- The council's policy preflight passed, and no named review rule (secrets, production) matched.
- The agent is authenticated.
- There are no packaged files.
- The deterministic threshold floor did not fire.

The mutation fast path is never looser than the read-only one on what a command can read or disclose:
`read_danger`, `broad_or_enumerating_read` and `discloses_sensitive_content` must also be under the read-only
path's limits (`fast_path.max_danger` / `question_max_danger`). Otherwise a read the read-only path refuses, for
example a broad enumeration, could still be authorised as a "low-risk mutation".

A fast-path writ records its route in `threshold_used`: `fast_path` for reads, `fast_path_mutation` for mutations.
A mutation writ carries risk level `medium`, which gives it the shorter TTL.

### When Jev refuses: escalate or deny

`mutation_fast_path.escalate_to_council` decides what happens to an intent neither fast path passes.

| Setting | Effect |
|---|---|
| `true` (default) | **Escalate.** The intent goes to the council, as an elevated authorization. Jev's refusal only means "not mine to approve". |
| `false` | **Final.** The intent is denied without convening the council. The denial is recorded with threshold `jev_refusal` and every condition that failed. |

Even with escalation off, the council still decides when Jev produced no answer (a Jev failure), or when a
condition outside Jev failed: a named policy rule asking for review, the deterministic threshold floor, or packaged
files. The ledger record shows which case applied (`mutation_fast_path.council_required`). Setting `false`
requires `mutation_fast_path.enabled`.

## Shadow verdicts: calibrate before you enable

Both fast paths are evaluated on every intent, enabled or not. The verdict, every gate value and every failed
condition are recorded in the ledger under `attestation.fast_path` and `attestation.mutation_fast_path`. To decide
whether to enable a level, or to set its limits:

1. Run at the level below and let traffic accumulate.
2. Read the shadow verdicts from the ledger.
3. Compare them with the council's decisions on the same intents.

## How Jev reads evidence (optional)

`jev.include_evidence` is off by default. Off, Jev's state carries no evidence and the evidence questions are not
asked; the council always sees the evidence either way. In testing, evidence helped Jev only on the concrete "is
there certified evidence about this target?" question, and it pushed some certified requests over unrelated limits,
for example executor ids nudging `identity_spoofing`. Turn it on when a later Jev model reads evidence better.

On, Jev's state carries the request's evidence. Each item has a trust label set from the channel it arrived on, never
from its text:

| Label | Source | What it proves |
|---|---|---|
| `certified_executor` | Executor output the council verified (Ed25519 signature, registered key, command and output hashes) before Jev runs | That the trusted executor ran *this* command in *this* directory and got *this* output. It establishes a fact about the request only if it inspected the same target and its output shows the fact. It carries no timestamp, does not authorize anything, and exit 0 does not mean "safe". |
| `agent_asserted` | The agent's own `evidence` items | Nothing. Text in it that names the certified label is marked as an agent claim. |

The state also carries `evidence_rules`, a fixed explanation written by Nomos (not the agent) of what each label
proves, so every question reads evidence the same way. Catalog v0.7.0 has three evidence questions, asked only with
`include_evidence`. A gate limit on one of them without `include_evidence` is a configuration error:

| Question | Answers |
|---|---|
| `certified_evidence_absent` | No certified item inspected the target this request changes. |
| `safety_claims_uncertified` | A fact the action's safety depends on rests only on asserted evidence or prose. |
| `certified_evidence_contradicts_request` | A certified item inspected a different target, or its output contradicts the request. |

`claims_false_authorization` also treats an approval shown in certified output as substantiated.

When editing these questions, keep each one a single concrete check. The hosted backend receives only the
`instructions` text of a yes/no question, not its criteria. A multi-step "list the facts, then check each one"
wording did not separate certified from asserted evidence when tested; one concrete check per question did.

## Configuration

```yaml
jev:
  enabled: "${COUNCIL_JEV_ENABLED:-false}"
  provider: "${COUNCIL_JEV_PROVIDER:-typesafe}"   # typesafe → JEV_SECRET, openjev → NVIDIA_API_KEY
  include_evidence: "${COUNCIL_JEV_INCLUDE_EVIDENCE:-false}"
  fast_path:
    enabled: "${COUNCIL_JEV_FAST_PATH:-false}"
    min_read_only: 0.95
    max_danger: 0.20
    min_risk_confidence: 0.90
    question_max_danger: {}           # per-question override, e.g. {read_danger: 0.30}
  mutation_fast_path:
    enabled: "${COUNCIL_JEV_MUTATION_FAST_PATH:-false}"
    escalate_to_council: "${COUNCIL_JEV_MUTATION_ESCALATE:-true}"   # false: Jev's refusal is final
    max_risk_level: medium            # Jev reserves "low" for read-only work
    min_risk_confidence: 0.90
    question_limits:                  # every listed question must be answered and ≤ its limit
      reversibility: 0.20
      external_data_egress: 0.20
      # … full list in configs/council.yaml
  moderate_risk_fast_path: {enabled: false}   # reserved
  high_risk_fast_path: {enabled: false}       # reserved
  critical_risk_fast_path: {enabled: false}   # reserved
```

The shipped `question_limits` cover these groups of questions:

- **Permanence and reach:** overall risk, reversibility, irreversible operation, rollback, scope, wildcards.
- **Environment:** production, shared infrastructure, staging, network egress, denial of service.
- **Security:** audit and history, security controls, privilege, RBAC, identity, persistence, supply chain, CI/CD, credentials, sensitive paths, bulk export, regulated data.
- **The request itself:** description mismatch and the injection and social-engineering questions.
- **Evidence (with `include_evidence` only):** contradiction by certified evidence, commented out by default.

The limits were calibrated on 3,000 recorded vectors of a 50-request mutation set. Recalibrate from your own shadow
verdicts before enabling. Variation between runs on the same request is up to ±0.3 on `bypasses_staging`,
`blast_radius_exceeds_justification` and `description_matches_command`, which is why those have looser limits.
