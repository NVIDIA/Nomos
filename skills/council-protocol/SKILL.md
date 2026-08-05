---
name: council-protocol
description: How to talk to the Nomos Security Council from an agent. Submit intents the council can reason about, read denial responses correctly, revise once when the framing was off, and accept denials when an action is not yours to perform. Load this skill before submitting privileged actions.
license: Apache-2.0
---

# Talking to the Council

The Security Council is a multi-member LLM jury that reviews each intent
before privileged actions run. It returns a decision plus reasoning.

The council is reasoning about the content of *this* intent, in this
call. There is no memory between calls, no behavioral-trust score, no
automated retry-storm detector, no operator alarm sitting on top of you.
The reasoning you get back comes from how the council reads what you
submitted right now.

That's the central thing: **the council reasons about the content you
give it**. Make that content truthful and specific.

## Two tools

| Tool | Use when |
|---|---|
| `council_propose_intent` | You're testing the council's reaction. Same evaluation as `authorize`, no writ minted. No commitment. |
| `council_authorize` | You're committed to running the action if approved. On approval you get a signed writ to submit to the trusted executor. |

Default to `propose_intent` when unsure. It's the cheapest way to learn
whether your framing is reviewable.

## What the council reads

When deciding, the members look at what you put in these fields:

- **`command`** — the exact string that will run.
- **`description`** — a sentence in plain English saying what the
  command does.
- **`target_resource`** — the specific thing acted on
  (`k8s://prod/api-gateway` beats "the cluster").
- **`target_environment`** — `development` / `staging` / `production` / `dr`.
  Tells the council how much risk to weigh.
- **`justification`** — why this action, and why now. Tickets,
  alerts, runbook references ground this.
- **`blast_radius`** — what could break and how widely.
- **`evidence`** (≤20) — log lines, command outputs, observed state.
- **`packaged_files`** (≤5) — files reviewed by the code-review
  sidecar; their hashes are signed into the writ.
- **`context`** — what led to this action (skip for atomic ops).
- **`agent_id`** + **`auth_token`** — your identity and proof of it.

Collapsed, a reviewable intent reads like:

> Agent `cascade` wants to run `kubectl rollout restart
> deployment/api-gateway -n prod` against `k8s://prod/api-gateway` in
> production. **Why:** cert rotation completed; pods need restart per
> runbook RB-0042. **Blast:** brief 503s during the rolling restart,
> ~30s. **Evidence:** [the rotation event from cert-manager], [the
> runbook step reference].

Every field carries factual weight. Nothing is fluff. Nothing
contradicts another field.

## Submission essentials

### Schema constraints

These are protocol limits — the request is rejected before reaching the
council if they're violated.

| Field | Required | Min | Max |
|---|---|---|---|
| `agent_id` | yes | 1 char, must match `^[a-zA-Z0-9_-]+$` | — |
| `auth_token` | yes (default deployment) | — | — |
| `command` | `authorize`: yes; `propose_intent`: no | `authorize`: 1 char | — |
| `working_directory` | `authorize`: yes; `propose_intent`: no | `authorize`: 1 char | — |
| `description` | `propose_intent`: yes | 10 chars | 16,000 chars |
| `target_resource` | `propose_intent`: yes | 1 char | — |
| `justification` | no | — | 32,000 chars |
| `blast_radius` | no | — | 16,000 chars |
| `context` | no | — | 32,000 chars |
| `evidence` | no | — | 20 items |
| `packaged_files` | no | — | 5 items |

Plenty of headroom on every text field. Length is not the variable —
content is.

### Content essentials

- **`command`** is the exact string to run. Use absolute paths where
  your operator's policy expects them. The writ binds the command into
  its signature, so what gets approved is what gets executed.
- **`description` reflects `command`.** Both are read by the same
  members; describe what the command actually does.
- **`target_resource`** names a specific thing, not a category.
- **`target_environment`** is honest. Don't tag prod work as
  `development`.
- **`blast_radius`** is honest. Underclaiming is a common deny reason —
  when blast_radius reads as smaller than the command obviously implies,
  members tend to say so directly in their reasoning.
- **`auth_token`** present. Missing or wrong → an `error` response
  (not a `denied`). Stop until the token is fixed; don't retry blindly.

## Reading the response

Always inspect:

- **`status`** — `"approved"`, `"denied"`, or `"error"`.
- **`guidance`** — list of concrete suggestions when denied.
- **`missing_permissions`** — scopes you don't have.
- **`council_members[].reasoning`** — what each member objected to,
  individually. Read all of them.
- **`chairman.risk_level`** — `low` / `medium` / `high` / `critical`.
- **`chairman.reasoning`** — threshold and risk framing.

Two paths from a denial: **revise** (the framing was off) or **accept**
(the action isn't yours to do). Choosing the right one is the whole
skill.

## When to revise

Revise when the denial is about *how the intent was framed*:

- Guidance items name fixable problems — more evidence, narrower
  target, rollback plan, absolute paths, accurate blast radius.
- Missing permissions are scopes that *should be in your role* — you
  forgot to include them, or your operator can issue them.
- Member reasoning critiques the *content* of the intent, not your
  *role*.

Then:

1. Read every `council_members[].reasoning`. Identify each concern.
2. Address each concern by adding accurate detail or correcting a
   mistake — narrower target, real ticket reference, honest blast
   radius.
3. Resubmit **once**.
4. If approved, proceed. If denied again, **stop**. A second denial
   after an honest revision means either you misread the original
   concern or the action shouldn't proceed in this form. Don't try a
   third time — the next call would be evaluated on its own content,
   and you've already given it your best honest framing.

### Worked revision

**Original (denied):**

```json
{
  "agent_id": "cascade",
  "command": "kubectl delete pod api-gateway-7f9c -n prod",
  "description": "Delete the bad pod",
  "target_resource": "the pod",
  "target_environment": "production",
  "justification": "It's stuck",
  "blast_radius": "Just one pod"
}
```

Council guidance: target too vague; blast_radius doesn't acknowledge
the brief 503s while a replacement spins up; justification doesn't
ground this — what alert or symptom?

**Revised (likely approval):**

```json
{
  "agent_id": "cascade",
  "command": "kubectl delete pod api-gateway-7f9c -n prod",
  "description": "Delete pod api-gateway-7f9c which is stuck in CrashLoopBackOff so the deployment controller spins up a fresh replacement.",
  "target_resource": "k8s://prod/pod/api-gateway-7f9c",
  "target_environment": "production",
  "justification": "Investigating PD alert P-2200 (api-gateway 5xx spike at 14:00 UTC). Pod api-gateway-7f9c shows CrashLoopBackOff for 8 minutes; OOMKilled per containerStatuses. Restart will let the deployment controller recreate it.",
  "blast_radius": "Brief loss of one of three api-gateway replicas during pod recreation (~10-15s); deployment controller spins up a replacement automatically. No data written.",
  "evidence": [
    "kubectl get pod api-gateway-7f9c -n prod -o jsonpath='{.status.containerStatuses[0].lastState.terminated.reason}' returned: OOMKilled",
    "PD alert P-2200 fired at 14:00 UTC; api-gateway 5xx spiked from 0.1% to 12% on /checkout"
  ]
}
```

Same action, more honestly described.

## When to accept (out-of-purview)

Accept when the denial is about *scope, role, or authority*:

- Reasoning uses language like "not within this agent's
  responsibilities," "should be performed by [other role]," "requires
  human review," "outside the agent's scope."
- Missing permissions are categorically not in your role — you're a
  read-only agent and the action requires `infra:destroy`, not "you
  forgot to ask."
- `chairman.risk_level: critical` combined with scope language.
- You revised once and were denied again on substantive grounds.

Acceptance is an active behavior, not a passive one:

1. **Stop.** No more council calls for this action in this turn.
2. **Surface the council's reasoning** to whoever asked you. Quote the
   chairman and at least one member verbatim. Don't paraphrase in a
   way that softens the message.
3. **Suggest the right next step** based on what the council said — a
   human in the loop, a different role, a change-ticket process.

A clean acceptance reads like:

> The council denied this with risk level **high**. Chairman: *"This
> deployment change should be performed via the change-ticket process
> by an SRE on-call, not by an autonomous agent."* Member reasoning was
> consistent. I'm not going to retry. If you want to proceed, the path
> is either (a) open a CHG ticket and have on-call SRE execute, or (b)
> ask your operator whether this agent's role should include this
> scope.

## Behaviors that aren't revisions

These are different actions dressed as the same one. The council
evaluates whatever you submit on its own content, and these moves
don't usually address what the previous denial actually said:

- **Substituting a different command** for the same effect. `rm -rf X`
  → `find X -delete` is the same operation in different syntax.
- **Stripping detail** to make the intent look smaller. The council
  reasoned about the original; less information is rarely more
  reviewable.
- **Padding text** without changing what the action does. More words
  don't change the underlying ask.
- **Lowering `target_environment`** to reduce the apparent risk. If
  it's prod, it's prod.

## Behaviors that aren't acceptance

After an out-of-purview denial:

- **Don't change `agent_id`** to bypass missing permissions. The
  action isn't appropriate for *that* agent's role either; identity
  isn't the variable.
- **Don't decompose.** Splitting a denied operation into smaller
  pieces that together do the same thing is the same action with extra
  steps.
- **Don't re-ask through the other tool.** `propose_intent` and
  `authorize` run the same evaluation. The verdict will be the same.
- **Don't loop back to your planner** with "what should I try
  instead?" hoping for a workaround. The "no" is the answer.

If the user genuinely needs the action done and it's out of your
scope, that's not a failure mode — that's the system working.
Privileged actions exist on a spectrum, and not every agent is
provisioned for every action. Your job is to *report* the boundary
clearly, not to route around it.

## Quick triage

| Response | What it means | What to do |
|---|---|---|
| `error: "Authentication failed..."` | Bad/missing `auth_token` | Ask your operator. Don't retry until fixed. |
| `status: "denied"`, framing-shaped guidance | Revisable | Revise once with concrete fixes. |
| `status: "denied"`, scope language in chairman/member reasoning | Out-of-purview | Stop. Report. |
| `status: "denied"`, second denial after revision | Out-of-purview by exhaustion | Stop. Report. |
| `status: "error"`, generic upstream/transport | Infra issue | One retry after a small backoff. Don't loop. |

## Disposition

The council is reasoning about your intent in the moment, not building
a profile of you over time. Three implications worth holding onto:

1. The point of a good intent is that *this* evaluation goes well —
   not that some future evaluation will be easier on you.
2. The point of accepting a denial cleanly is that the denial *was
   correct in this case* — acting on it is the right behavior.
3. The point of not-substituting / not-decomposing isn't that you'll
   get caught — it's that those moves don't actually answer what the
   council said. The next call's verdict depends on the next call's
   content, and the same kind of intent gets the same kind of verdict.

Be straightforward with the council and the responses become a useful
signal. Treat it as something to game and the responses become noise.
