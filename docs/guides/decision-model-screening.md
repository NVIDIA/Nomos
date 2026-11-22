<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Route decision-model findings to denial or human approval

Decision-model screening lets an operator attach an explicit consequence to a
particular yes/no question. For example, installation can require a person’s
approval while unauthorized credential disclosure is denied. The model supplies
probabilities; the deployment configuration supplies the policy.

Screening is **off by default**. With screening disabled, System One keeps its
existing attestation, chairman, and fast-path behavior. Enabling it first in
`shadow` mode records what the rules would decide without changing the ordinary
authorization result. Use `enforce` only after evaluating the questions and
thresholds on representative operations.

## Before and after

Before, an answer could warn Council about a package installation, but did not
make a human decision mandatory:

```text
shell.run(command="pip install example-package")
  → OPA permits further review
  → decision model reports installation
  → Council decides whether to authorize
```

After enabling an installation screening rule in enforcement mode:

```text
shell.run(command="pip install example-package")
  → OPA permits further review
  → package_installation reaches its configured threshold
  → status: requires_human_approval
  → application displays its existing approval UI; the tool has not run
```

An unauthorized-disclosure rule instead returns `denied`. Neither a Council vote
nor a human decision overrides an enforced hard denial. A clear screening result
does not authorize execution: the operation continues through the existing
Council or explicitly enabled fast-path checks.

These examples use `council_authorize`, whose response exposes `status` and
`guidance`. The proposal-only `council_propose_intent` API exposes an enforced
screening outcome through `state`, `error`, and `requires_approval`; it does not
mint a writ or create the application’s human-approval card. Its existing OPA
refusal response remains separate from that screening result. Applications
must use the appropriate API contract rather than interpret every refusal as
a human-approval request.

## Configure a limited question set

[`configs/screening_questions.yaml`](../../configs/screening_questions.yaml)
contains four adverse-framed yes/no questions plus the existing read-only gate
required by the catalog format. The gate is not configured as a screening rule.
The catalog deliberately omits the general catalog’s risk and fast-path
questions; leave both fast paths disabled and use the ordinary LLM chairman.

This configuration belongs in the Council deployment’s `council.yaml`, not the
tool arguments or worker manifest. The paths must exist inside the deployed
Council process or container.

```yaml
system_one:
  enabled: true
  provider: nemotron        # Or a compatible external decision-model endpoint.
  catalog_path: /etc/nomos/screening_questions.yaml
  screening:
    enabled: true
    mode: shadow           # Record first; change to enforce after evaluation.
    # default_action: deny # This is the default even when omitted.
    rules:
      package_installation:
        threshold: 0.90
        action: requires_human_approval
      outside_workspace_access:
        threshold: 0.90
        action: requires_human_approval
      sensitive_path_access:
        threshold: 0.90
        action: requires_human_approval
      unauthorized_credential_disclosure:
        threshold: 0.90
        # No action: inherits default_action, which defaults to deny.
  fast_path:
    enabled: false
  mutation_fast_path:
    enabled: false

chairman:
  provider: llm
  # Retain the deployment’s existing chairman backend and voting settings.
```

**The example’s 0.90 thresholds are illustrative, not calibrated defaults.** Each
configured rule requires an explicit threshold in `[0, 1]`. Evaluate false
positives, missed violations, ambiguous inputs, latency, and model outages before
choosing deployment thresholds. Changing a model or question wording requires
fresh evaluation. See [System One provider configuration](../system-one.md#choosing-which-system-one)
for endpoint, model-version, and private API-key settings.

## What triggers a rule

| Setting or result | Meaning |
|---|---|
| `rules.<question_id>.threshold` | Trigger when the question’s probability is **greater than or equal to** this value. |
| `rules.<question_id>.action: deny` | A triggered rule returns `denied`; the operation must not run. |
| `rules.<question_id>.action: requires_human_approval` | A triggered rule returns `requires_human_approval`; the application owns the approval UI. |
| Omitted rule action | Use `screening.default_action`. |
| Omitted `default_action` | Use `deny`, but only for a rule that actually triggers. |
| No configured rule triggers | Continue through the existing authorization path. This is not an approval. |

Only question IDs explicitly listed in `screening.rules` participate in this
policy. Other answers remain informational or inputs to the existing routing
features. Catalog labels such as `flag` and `deny-candidate` are metadata; they
do **not** select an enforcement action. The rule configuration does that.

The initial screening contract accepts adverse-framed `noul` questions: high
means the concerning condition is more likely. Unsupported actions, unknown
question IDs, incompatible question types, and invalid thresholds are rejected
during startup. Enforced screening must not silently disable itself when a
catalog cannot load.

## Preserve denial precedence

An OPA hard denial stops the request without a model call. If OPA requests human
approval, an enforced decision-model hard denial still takes precedence over
that ask. If both model-deny and model-HITL rules trigger, denial wins. Otherwise
an existing OPA ask or a triggered model-HITL rule requires human approval before
the application may execute the action.

In `shadow` mode, these are recorded as hypothetical screening outcomes; the
ordinary OPA/Council behavior remains in force. Shadow mode does not grant a
permission or remove an existing human-approval requirement.

## Recheck only hard-denial questions after human approval

Applications that already verified approval for the exact operation can use the
existing `policy_only` inspection path. This is not an authorization bypass:
the application still owns identity, expiry, operation binding, and final
argument verification.

```text
First proposal:
  OPA + decision-model screening
    → requires_human_approval
    → application saves and obtains approval for that exact operation

Approved replay:
  application validates the saved approval and final operation
    → policy_only rechecks OPA and effective-deny screening questions
    → hard denial: stop
    → checks pass: policy_allowed; application may continue its verified replay
```

The replay model request includes **only** rules whose effective action is
`deny`, resolving omitted actions through `default_action` first. It does not
repeat HITL questions or the full catalog, and sends no model request if there
are no effective-deny rules. A human-approved replay therefore does not create
another installation or workspace approval loop. `policy_allowed` grants no
Council writ or independent execution authority. OPA can still report its
existing `requires_human_approval` rule on inspection; an integration that has
verified approval for the exact operation treats that requirement as satisfied,
not as a request to create another card. A current denial or screening error
still stops replay.

## Missing answers and audit records

An unavailable model, malformed or missing required answer, wrong answer type,
or nonnumeric/out-of-range probability cannot establish that an operation is
safe. In `enforce` mode these failures return `error`, prevent execution, and do
not create a human-approval request. In `shadow` mode the failure is recorded and
the ordinary authorization path continues. A catalog/configuration error is a
startup error, not a reason to turn enforced screening off.

The audit record includes the screening mode, selected questions, thresholds,
effective actions, scores when available, and screening outcome alongside the
model/catalog versions. Keep shadow evidence when moving to enforcement so the
deployed policy can be traced to the evaluated configuration.

## Supply runtime facts and retain execution controls

Workspace questions need the host-validated workspace mapping and resolved
working directory supplied by the [runtime-context contract](tool-runtime-context.md).
Working directory and workspace root are different concepts. An agent-written
`target_resource`, a tool description, or a claim that a path is safe cannot
establish the authorized workspace. Sensitive files can also exist inside a
workspace, so sensitive access and outside-workspace access remain separate
questions.

Reading a credential for legitimate authentication is not the same as sending
it to an unauthorized recipient. The limited catalog separates those questions
so an operator can require human approval for sensitive access without treating
every credential read as exfiltration.

Model screening is probabilistic. A named script whose contents are absent,
symlink resolution, changed files, or an unknown destination may require facts
the model does not have. These questions do not replace runtime confinement,
credential controls, network enforcement, or the application’s verification of
the final action.
