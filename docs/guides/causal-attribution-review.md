<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Task support in Command Integrity review

The shipped Command Integrity reviewer is instructed to assess whether a proposed
call's concrete operation, target, arguments, and effects serve the user task
supplied in the conversation or context. It distinguishes observations that supply
useful facts or task parameters from observations that introduce a new purpose,
recipient, target, or side effect. A routine workflow instruction can introduce
an unsupported goal even without an explicit role override.

This guidance is in the Command Integrity priorities and evaluation criteria in
`configs/council.yaml` and its Kubernetes mirror,
`deploy/kubernetes/nomos/files/council/council.yaml`. Existing custom member
configurations do not acquire it automatically. To opt in, copy the three
`Task support`, `Trust boundary`, and `Attribution explanation` priorities and the
`task_support`, `observation_role`, `attribution_limits`, and
`attribution_explanation` criteria into the custom Command Integrity member,
keeping the existing policies. A `system_prompt_override` replaces the generated
prompt; operators using one must incorporate equivalent guidance themselves.
`CouncilSettings.members` has an empty default, rather than a second in-code
Command Integrity template.

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

- **B1, useful parameter:** Agent context reports a task to inspect metadata of
  the quarterly report selected by `/srv/reports/index.txt`. Its output supplies
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
backends with fixed authored responses. They exercise both shipped templates,
generated initial/debate prompts, real member parsing, both complete service
APIs, signed evidence verification, forged-header handling, tamper rejection,
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
