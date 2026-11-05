<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Task support in the opt-in chat-agent profile

The bespoke [chat-agent profile](../../configs/use-cases/chat-agent/council.yaml)
adds task-support guidance to Command Integrity review for a chat application
receiving direct user requests. The reviewer is instructed to connect a proposed
call's concrete operation, target, arguments, and effects to the latest user
request and relevant prior conversation supplied by the application. Observations
may supply useful facts or parameters; embedded directions cannot expand that
task with a new purpose, recipient, target, or side effect. A routine workflow
instruction can introduce an unsupported goal without an explicit role override.

This is an explicit opt-in configuration with id `council-chat-agent`. Normal
`configs/council.yaml` and its Kubernetes mirror retain their original contents.
There is no autoload, directory discovery, default-catalog enrollment, or automatic
rollout to existing custom profiles and running or seeded deployments. The
profile preserves every original setting except its distinct id and three added
priorities/four criteria on Command Integrity. There is no system prompt override,
new runtime stage, schema field, policy, model mapping, or voting mechanism.

## Select the profile

From the repository root, the existing standalone interactive `propose_intent`
runner accepts the complete profile directly:

```sh
uv run python -m nomos.nat.run --config configs/use-cases/chat-agent/council.yaml
```

For the existing NAT MCP workflow, use its `COUNCIL_CATALOG_PATH` substitution;
the catalog loader accepts this complete single-council file:

```sh
COUNCIL_CATALOG_PATH=configs/use-cases/chat-agent/council.yaml uv run --extra nat nat mcp serve --config_file configs/council_nat_workflow.yaml
```

The application sends `council_id: "council-chat-agent"`. For explicit MCP
selection, use `council_v2_propose_intent` and `council_v2_authorize` with
`api_version: "v2"` and that id. An id alone does not load an absent profile;
unknown or disabled ids fail through existing routing. A dedicated process uses
its explicitly loaded single profile as its process default. The standalone
command is an interactive runner; the second command serves MCP.

Both commands need the normal provisioned deployment environment: credentials for
all provider variables referenced by the loader, an authenticated agent with
membership in this distinct council and required permissions, Redis credentials
and account-integrity key, available OPA, and writable configured key/ledger
locations. The MCP command additionally needs optional NAT dependencies. The
profile inherits the base service addresses and runtime paths; provision these
without disabling existing gates or editing checked-in runtime settings to make
an example run. See the [use-case index](../../configs/use-cases/README.md).

## Supply direct user chat context

The chat application supplies the actual latest user request and relevant prior
conversation through the existing `context: str`. Label sources and turns, and
state omissions or truncation explicitly. Keep tool/document observations in the
existing `evidence: list[str]` or genuine `certified_evidence` channel. If an
observation must be quoted in context, label it as observation rather than a user
instruction. No new request field or authenticated human channel is introduced.
For example, this fragment can accompany the usual command/description/target/
justification/authentication fields:

```json
{
  "api_version": "v2",
  "council_id": "council-chat-agent",
  "context": "Latest user request (chat turn 7, supplied by application): Inspect quarterly report metadata. Relevant prior conversation (turn 5, supplied by application): Use the report selected by /srv/reports/index.txt. Omitted history: none.",
  "evidence": ["Observation from index.txt: selected_path=/srv/reports/quarterly.txt"]
}
```

This is a transport example, not a trusted attestation. The council authenticates
an agent account and council permissions, not a human transcript's origin or
consent. Application-supplied turn labels and user quotations remain agent-asserted
from the council's perspective. Missing, conflicting, or truncated history should
remain visible as uncertainty.

## What operators see

The reviewer is asked for a concise explanation in its existing `reasoning`
field: name the supplied task and source, connect the call to it or explain the
mismatch, describe the observation's role, and state material uncertainty. The
existing instruction to provide at least two full sentences remains. This is an
operator rationale, not a request for a hidden reasoning transcript.

The existing member parser preserves that text as `IntentEvaluation.reasoning`.
`CouncilService.propose_intent()` exposes the Command Integrity `MemberVote.reasoning`
in `ProposeIntentOutput.council_members`; `CouncilService.authorize()` exposes it in
`AuthorizeOutput.council_members`. Both serialized responses expose these votes in
`council_members`. No new response keys or attribution labels are introduced.
The parser enforces the existing non-empty reasoning requirement; it does **not**
enforce the semantic content of this new explanation rubric.

## Trust boundaries and existing policy

Descriptions, justifications, context, and plain evidence are agent-asserted,
including text labeled as a user message or approval. A document, tool result,
previous debate vote, or claim of user/system authority does not become an
instruction just by being quoted. Missing, truncated, contradictory, or merely
agent-asserted task provenance should be stated as uncertainty; the reviewer
should not invent a user instruction or call the task verified.

Certified executor evidence attests what the trusted executor observed under the
existing signature, registered-key, and key-security assumptions. It does not
make instructions within the output authoritative, authorize a new purpose, or
prove why an agent proposed a call. A forged certified header in plain evidence
remains agent-asserted; genuine certification uses the separate verified evidence
channel. Task-relevant observed facts and parameters remain usable within the
stated task and existing policies.

Unknown task provenance alone is not a reason to deny a well-scoped,
well-reasoned evidence-gathering read. This preserves the existing read-only
pathway. Task support also does not independently authorize a call: privileged
reads, changes, certified safety evidence, permissions, and other reviewer
requirements still apply. An unsupported-goal judgment is an ordinary deny vote
under existing aggregation, not a new veto, permission grant, or voting stage.

The optional reasoning check asks whether a purpose remains supported when
observation directives are treated only as data, while relevant facts and
parameters remain available. It is hypothetical explanatory reasoning, not replay
or experimental evidence.

## Illustrative authored explanations

These examples are written by the authors; they are **not observed model
outputs**, measured decisions, or proof that a deployed reviewer follows the
instructions. Paths are synthetic data, and the examples execute nothing.

- **B1, useful parameter:** Application-supplied latest-request context reports
  a task to inspect metadata of the quarterly report selected by `/srv/reports/index.txt`. Its output supplies
  `selected_path=/srv/reports/quarterly.txt`; the proposed call is
  `/usr/bin/stat /srv/reports/quarterly.txt`. An illustrative rationale:
  "The agent-reported quarterly task in context supports stat on the selected
  quarterly report. The index supplies a target parameter rather than a new
  purpose; the task source remains agent-asserted."
- **D1, added purpose:** With the same quarterly task, the index additionally
  says routine compliance requires annual-report inspection, and the proposed
  call becomes `/usr/bin/stat /srv/reports/annual.txt`. An illustrative rationale:
  "The supplied quarterly task does not support annual-report inspection. Only
  the index's routine-compliance direction introduces that purpose, so it cannot
  expand the task."
- **U1, missing source:** Prior conversation is truncated, no evidence is
  available, and the justification narrowly requests quarterly metadata to
  establish scope. An illustrative rationale: "No prior user task source is
  supplied, so task origin is uncertain. This narrowly justified metadata read
  gathers scope evidence; missing provenance alone does not disqualify it,
  subject to existing policies."

An explicit supplied annual-metadata task can support the same annual call as
D1; the target alone does not establish an observation-introduced goal. Likewise,
signing either index result changes observation provenance, not instruction
authority. A supported config-update purpose still needs required certified
safety evidence and permissions.

## Verification and research limits

`tests/test_causal_attribution_review.py` and
`tests/fixtures/causal_attribution_review.json` use offline synthetic recording
backends with fixed authored responses. They check byte-exact default isolation
and the bounded opt-in profile delta, generated initial/debate prompts for the
chat profile, real member parsing, both complete service APIs, signed evidence verification, forged-header handling, tamper rejection,
and the existing permission gate. Scripted reasoning returning through these
paths proves transport and parsing only. These checks do not measure model
compliance, decision quality, attack success rate, or security robustness; no
live or paid inference is part of this feature's verification.

[AttriGuard (USENIX Security 2026)](https://www.usenix.org/system/files/usenixsecurity26-he-yu.pdf),
sections 3.2 and 4.1–4.4, motivates distinguishing user-task purpose from
observation-provided information. Its mechanism changes observation views,
reuses action history, queries the original agent policy with teacher-forced
replay, and compares calls using a fuzzy survival criterion. This feature
implements none of those interventions. A contextual reviewer explanation is
not verified causal attribution, counterfactual replay, or an AttriGuard
implementation. Even signed host-observed text would attest captured text,
not promote its quoted directions into authority.

A future separately authorized evaluation could grade actual model outputs
against the fixture rubric while recording models, inputs, outputs, and failures.
That would measure reviewer behavior; establishing agent causality or replay
requires additional machinery outside this feature.
