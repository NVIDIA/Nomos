<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# System one (member zero)

System one is a fast classifier that runs once per intent, after OPA and before the council. It answers a versioned
question catalog about the request in one call. It never votes. Depending on the level you enable, its answers
inform the council, set the chairman's risk level and threshold, or authorize the intent without convening the
council.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/diagrams/system-one-dark.svg">
  <img alt="System one and the council. After OPA has run and allowed a request, system one makes one call that answers a versioned question catalog; it never votes. With a fast path enabled, a read that passes the read-only gate and every limit, or a low-risk change, gets a Writ with no council. Every other request goes to the council: the chairman is either an LLM call or, with chairman.provider system_one, derived from the same answers with no call, and the voting members see every answer beside the request. When system one gives no answer, the council decides and a system one chairman falls back to unanimous and high. Every answer and both fast-path verdicts go to the ledger record. The system_one.provider setting selects the backend: nemotron, the default, with its endpoint and model built in and the key NVIDIA_API_KEY, or external, your own endpoint with base_url, model_version and the key SYSTEM_ONE_API_KEY." src="assets/diagrams/system-one.svg">
</picture>

System one is off by default, and each level is its own setting. To turn it on you choose two things: [which
system one answers](#choosing-which-system-one) and [how much its answers decide](#levels).

Without enforced policy screening, a system one failure sends the intent to the council without a system one
record, and a [system one chairman](#the-system-one-chairman) falls back to unanimous / high. Opt-in
[decision-model screening](guides/decision-model-screening.md) can instead make selected answers require human
approval or denial. Its enforcement mode returns an error if required answers are unavailable; shadow mode
preserves ordinary behavior. Screening is separate from the approval fast paths and is off by default.

The request is sent to the system one backend with common credential shapes (keys, tokens, passwords) masked, field
by field. Masking never touches what a writ covers: if it would alter the command, the operation or the tool
arguments, nothing is sent. The request follows the applicable failure behavior above. Answers about a masked
command would be answers about a different request.

## Choosing which system one

`system_one.provider` selects the backend and the key it authenticates with. Nothing else in the configuration, and
nothing in the question catalog, depends on the choice.

| Provider | What it is | Endpoint and model | Key |
|---|---|---|---|
| `nemotron` (default) | NVIDIA-hosted Nemotron System 1 | Built in: `https://inference-api.nvidia.com`, model `nvidia/nemotron-deciffusion/nemotron-deciffusion-8b` | `NVIDIA_API_KEY`, the key the `nv_inference` council backends already use |
| `external` | A system one endpoint you supply | None built in. You must set `base_url` and `model_version`; the council refuses to start without them | `SYSTEM_ONE_API_KEY` |

Set the provider wherever you set the rest of the council's configuration:

| Deployment | Provider | Endpoint | Model | Key |
|---|---|---|---|---|
| `council.yaml` | `system_one.provider` | `system_one.base_url` | `system_one.model_version` | `system_one.api_key`, or leave it blank to read the provider's key variable |
| Environment (Compose, deploy script) | `COUNCIL_SYSTEM_ONE_PROVIDER` | `COUNCIL_SYSTEM_ONE_BASE_URL` | `COUNCIL_SYSTEM_ONE_MODEL` | `NVIDIA_API_KEY` or `SYSTEM_ONE_API_KEY` |
| Helm | `council.systemOne.provider` | `council.systemOne.baseUrl` | `council.systemOne.model` | `secrets.stringData.nvidiaApiKey` or `secrets.stringData.systemOneApiKey` |

Nemotron, with everything left at its default:

```yaml
system_one:
  enabled: true
  provider: nemotron          # key from NVIDIA_API_KEY
```

Your own endpoint:

```yaml
system_one:
  enabled: true
  provider: external          # key from SYSTEM_ONE_API_KEY
  base_url: https://system-one.internal.example
  model_version: example-model-1.2.0
```

Rules that apply to either provider:

- **An explicit value wins.** A `base_url`, `model_version` or `api_key` you set is used as written. Only a blank
  field falls back to the provider's default, so you can point `nemotron` at a private deployment of the same model
  by setting `base_url` alone.
- **Pin a concrete model version.** A name ending in `-latest` or `-preview` is rejected. The version is recorded on
  every attestation vector, and the limits are tuned to it.
- **The request path is `system_one.endpoint_path`**, `/v1/systemone` by default, appended to `base_url`.
- **Strict profiles.** With `NOMOS_DEPLOY_PROFILE=alpha` the council refuses to start when system one is enabled and
  the key is missing, or `base_url` is not an `https://` URL. Every request carries the key.

### What an endpoint has to speak

Both providers send the same request: the model version, the masked request state, and the questions, each with its
type (`noul` for yes/no, `score`, or `choice`), its instructions and its criteria. They expect one answer per
question, with a value and, for `score` and `choice`, a confidence. The two providers differ in two ways:

| | `nemotron` | `external` |
|---|---|---|
| Yes/no (`noul`) questions | Sent with their two-key criteria, which the model needs | Sent with instructions only; the criteria are dropped |
| Questions per request | The whole catalog in one call | The whole catalog in one call; set `system_one.max_questions_per_request` if the endpoint caps it |

This is not a general chat-completion interface, and a model that speaks the contract is not interchangeable with
another one. Scores from two models are not on the same scale. After changing the provider or the model version,
read the [shadow verdicts](#shadow-verdicts-calibrate-before-you-enable) and re-tune the limits before enabling a
fast path.

## Levels

These levels describe approval delegation, not mandatory screening. When enabled in enforcement mode,
[screening rules](guides/decision-model-screening.md) run before an approval fast path or Council review can
authorize the operation. A triggered denial or human-approval requirement cannot be waived by either.

The levels stack. Each needs every level below it. The council refuses to start with a level enabled and one
below it disabled, so a misconfiguration is an error rather than a silent no-op.

| Level | Setting (`system_one.` in `council.yaml`) | Env / Helm value | What it adds |
|---|---|---|---|
| 1 | `enabled` | `COUNCIL_SYSTEM_ONE_ENABLED` / `council.systemOne.enabled` | **Prefilter.** Attestations are attached to the intent, shown in full to every council member and recorded in the ledger. With `chairman.provider: system_one`, system one also sets the risk level and threshold. Every intent still goes to the council. |
| 2 | `fast_path.enabled` | `COUNCIL_SYSTEM_ONE_FAST_PATH` / `council.systemOne.fastPath` | **Read-only fast path.** A read-only intent gets a writ without the council when system one's read-only gate, risk level and adverse read questions all pass. |
| 3 | `mutation_fast_path.enabled` | `COUNCIL_SYSTEM_ONE_MUTATION_FAST_PATH` / `council.systemOne.mutationFastPath` | **Low-risk mutation fast path.** A state-changing intent gets a writ without the council when every listed question is answered and under its limit: local, reversible, narrowly scoped work. |
| 4–6 | `moderate_risk_fast_path`, `high_risk_fast_path`, `critical_risk_fast_path` | none | **Reserved.** The settings exist so the level order is fixed now. No router exists for them, and `enabled: true` is rejected. |

Enabling a fast path is a trade-off the operator makes: a request that meets the configured criteria gets a writ on
system one's answers alone, and no council member reads it. The mutation path checks only the questions listed in
`question_limits`, and system one answers from the request text, so it rates a script or manifest named only by path
without seeing its content. The shipped limits therefore send a change whose payload is not visible in the request
(`opaque_command_payload`), or that deletes or replaces existing data (`deletes_or_overwrites_data`), to the council.

Both fast paths also require all of these:

- OPA ran and allowed the intent. With `opa.fail_closed: false`, an intent let through because OPA could not be
  reached goes to the council.
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

### When system one refuses: escalate or deny

`mutation_fast_path.escalate_to_council` decides what happens to an intent neither fast path passes.

| Setting | Effect |
|---|---|
| `true` (default) | **Escalate.** The intent goes to the council, as an elevated authorization. System one's refusal only means "not mine to approve". |
| `false` | **Final.** The intent is denied without convening the council. The denial is recorded with threshold `system_one_refusal` and every condition that failed. |

Without enforced screening, even with escalation off, the council still decides when system one produced no
answer (a system one failure), or when a condition outside system one failed: a named policy rule asking for review, the deterministic threshold
floor, or packaged files. The ledger record shows which case applied (`mutation_fast_path.council_required`).
Setting `false` requires `mutation_fast_path.enabled`.

## The system one chairman

The chairman does not vote. It assesses risk and chooses the approval threshold for the members' vote, and the risk
level it reports also sets the writ's lifetime. `chairman.provider` selects who does that:

| `chairman.provider` | How the decision is made | Needs |
|---|---|---|
| `llm` (default) | One call to the model named by `chairman.backend`, guided by the chairman prompt | A chairman backend |
| `system_one` | No call of its own. The risk level and the threshold are computed from the answers system one already gave for this intent | `system_one.enabled` (level 1). The council refuses to start otherwise |

A system one chairman decides in this order:

1. **Risk level.** It takes system one's `risk_level` answer. A `low` or `medium` answer with confidence under
   `chairman.system_one.min_risk_confidence` (0.5) is reported as `high`: the risk level sets the writ's lifetime,
   and an unsure "low" would grant the longest one.
2. **Threshold.** `unanimous`, unless every one of these holds, in which case it is `quorum`:
   - `chairman.allow_quorum` is `true`. With the shipped `false`, the threshold is always unanimous.
   - The reported risk level is `low`.
   - The confidence in that answer is at least `chairman.system_one.quorum_min_confidence` (0.9).
   - The read-only gate's probability is at least `chairman.system_one.quorum_min_read_only` (0.9).
3. **The deterministic floor wins.** A request the built-in threshold floor marks unanimous stays unanimous, with
   at least the floor's risk level, whatever system one answered.
4. **No usable answers means unanimous / high when the request reaches the chairman.** Enforced screening stops
   first with an error when a required answer is unavailable. The chairman fallback covers a system one failure,
   a request withheld by masking, and a vector with the risk answer or the read-only gate missing.

The chairman is the one place a system one answer can relax scrutiny at level 1, by choosing quorum over unanimous.
The request's prose is written by the agent, which is why quorum needs all of the conditions above and not the risk
answer alone.

The decision is recorded like any chairman's. Its provider is `system_one:<model_version>`, and its reasoning gives
the answered risk level and confidence, the read-only probability and the threshold chosen.

```yaml
chairman:
  enabled: true
  provider: system_one          # llm (default) or system_one
  allow_quorum: false           # true lets the chairman choose quorum
  system_one:
    min_risk_confidence: 0.5    # below this, low or medium is reported as high
    quorum_min_confidence: 0.9
    quorum_min_read_only: 0.9
```

The chairman and the fast paths are independent. A fast path is decided before the council is convened, so a
request it authorizes never reaches the chairman; every other request does, whichever provider the chairman uses.

## Shadow verdicts: calibrate before you enable

Both fast paths are evaluated on every intent, enabled or not. The verdict, every gate value and every failed
condition are recorded in the ledger under `attestation.fast_path` and `attestation.mutation_fast_path`. To decide
whether to enable a level, or to set its limits:

1. Run at the level below and let traffic accumulate.
2. Read the shadow verdicts from the ledger.
3. Compare them with the council's decisions on the same intents.

## How system one reads evidence (optional)

`system_one.include_evidence` is off by default. Off, system one's state carries no evidence and the evidence
questions are not asked; the council always sees the evidence either way. In testing, evidence helped system one
only on the concrete "is there certified evidence about this target?" question, and it pushed some certified
requests over unrelated limits, for example executor ids nudging `identity_spoofing`. Turn it on when a later
system one model reads evidence better.

On, system one's state carries the request's evidence. Each item has a trust label set from the channel it arrived
on, never from its text:

| Label | Source | What it proves |
|---|---|---|
| `certified_executor` | Executor output the council verified (Ed25519 signature, registered key, command and output hashes) before system one runs | That the trusted executor ran *this* command in *this* directory and got *this* output. It establishes a fact about the request only if it inspected the same target and its output shows the fact. It carries no timestamp, does not authorize anything, and exit 0 does not mean "safe". |
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

When editing these questions, keep each one a single concrete check. The `external` provider receives only the
`instructions` text of a yes/no question, not its criteria. A multi-step "list the facts, then check each one"
wording did not separate certified from asserted evidence when tested; one concrete check per question did.

## Configuration

```yaml
system_one:
  enabled: "${COUNCIL_SYSTEM_ONE_ENABLED:-false}"
  provider: "${COUNCIL_SYSTEM_ONE_PROVIDER:-nemotron}"   # nemotron → NVIDIA_API_KEY, external → SYSTEM_ONE_API_KEY
  base_url: "${COUNCIL_SYSTEM_ONE_BASE_URL:-}"           # blank: the provider's endpoint (external has none)
  model_version: "${COUNCIL_SYSTEM_ONE_MODEL:-}"         # blank: the provider's model (external has none)
  timeout_ms: "${COUNCIL_SYSTEM_ONE_TIMEOUT_MS:-1500}"   # past it, the intent goes to the council
  include_evidence: "${COUNCIL_SYSTEM_ONE_INCLUDE_EVIDENCE:-false}"
  fast_path:
    enabled: "${COUNCIL_SYSTEM_ONE_FAST_PATH:-false}"
    min_read_only: 0.95
    max_danger: 0.20
    min_risk_confidence: 0.90
    question_max_danger: {}           # per-question override, e.g. {read_danger: 0.30}; each must be answered
  mutation_fast_path:
    enabled: "${COUNCIL_SYSTEM_ONE_MUTATION_FAST_PATH:-false}"
    escalate_to_council: "${COUNCIL_SYSTEM_ONE_MUTATION_ESCALATE:-true}"   # false: system one's refusal is final
    max_risk_level: medium            # system one reserves "low" for read-only work
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
- **The request itself:** description mismatch, a payload that is not visible in the request, deletion or replacement of existing data, and the injection and social-engineering questions.
- **Evidence (with `include_evidence` only):** contradiction by certified evidence, commented out by default.

The limits were calibrated on 3,000 recorded vectors of a 50-request mutation set. Recalibrate from your own shadow
verdicts before enabling. Variation between runs on the same request is up to ±0.3 on `bypasses_staging`,
`blast_radius_exceeds_justification` and `description_matches_command`, which is why those have looser limits.
