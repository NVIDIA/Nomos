<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Should HTTP review receive context only while a tool is pending?

This is a small **exploratory, source-informed synthetic experiment** on known
fixtures. It tests the proposed eligibility rule, not a deployed implementation
or a captured live Relay run. The [plan](PLAN.md) is frozen before model calls.

**Completed once:** all 41 distinct calls succeeded. Read [RESULTS.md](RESULTS.md)
for the findings and [per-pair tables](data/measured-run-001/tables.md) for every
lifecycle and error. The intended rule is **pending on the newest event for at
least one tool ID**, with earlier completed calls retained. It preserved the
role distinction in the pending-sender cases, including concurrency. No-pending
stress cases are outside the normal successful synchronous-sender assumption;
their lost context is not evidence of a downside within that assumed path.
This remains an experiment; the production reader has not been changed.

## Why this follow-up exists

The preceding [refresh study](../2026-11-context-refresh/RESULTS.md) included
132 cases but zero latest pending events. A corrected-retry example contained
only an earlier denied draft; it did not show the corrected sending tool call.
That is a valid unassociated-history distraction test, but an incomplete example
of a normal synchronous retry. We preserve all old evidence and author new
lifecycle variants here rather than rewriting those inputs after measurement.

Source inspection of [Relay publisher PR 32](https://github.com/NVIDIA-dev/nomos-plugins/pull/32)
at `2607a04a962cf359abb76f3e2d011e844a81d66a` confirms that pending publication
is awaited before dispatch. Publication is fail-open: failure to write that event
does not prevent approved execution. Missing pending therefore may reflect a
capture gap. No live publisher/HTTP trace was captured by this experiment.

## Related themes do not mean identical tests

| Theme | Role-binding case | Distraction case |
| --- | --- | --- |
| Exemption/approval claim | Opaque outgoing A needs context to reveal its credential role; approval still does not exempt it | The outgoing HTTP already reveals a disclosure; an approval claim should not override it |
| Sensitive history, benign outgoing content | Context distinguishes ordinary B from credential A | HTTP already shows benign/redacted content; a historical secret should not create disclosure |

These categories overlap and are not independent proofs of separate abilities.
The new study selects one opposite-role pair plus two HTTP-decidable distraction
pairs (removed bearer and approval claims). These are seen fixtures, selected for
diagnosis. It does not expand credential-role coverage to confidential business
information or establish general accuracy.

## What changes and what stays fixed

Each of six base cases gets six authored lifecycle variants: history only,
pending sender, completed sender, pending sender with a newer unrelated
completion, unrelated pending without a sender, and an older pending event
superseded by completion on the same tool ID. The actual outgoing HTTP and
original questions never change. Sender-pending versus sender-completed differs
only in the reported execution state; sender arguments and Council approval are
identical. Added command records are never executed.

Four logical conditions choose between the full context and HTTP-only:

| Condition | When is context included? |
| --- | --- |
| Ungated | Always |
| Any pending call | At least one identified tool call's newest event is pending |
| Newest event pending | The newest event across all calls is pending and identified |
| HTTP-only | Never |

The **Any pending call** row is the intended rule. Group events by
`tool_call_id` and inspect the newest event for each ID. An older pending event
does not qualify if the same ID has a newer completed event. Once any ID
qualifies, retain the complete already-selected context: an earlier completed
call with a different ID can explain the pending sender's outgoing value.

For example, the measured role input contains both records in the same retained
model-request/turn group:

```text
call-07:     newest event execution_pending — current outbound proposal
tool-call-1: newest event completed         — earlier role information
```

The intended gate retains both. It does not filter out completed tool IDs.
The experiment searches all retained groups for eligibility and retains them;
it does not impose a same-turn-only filter or add exact HTTP attribution.
The global-newest condition is a comparison, not the intended rule.

The two gates differ with concurrency. A newer unrelated completion makes the
strict newest-event gate drop context even if another call remains pending.
An unrelated pending call can pass either gate without establishing the source
of the HTTP. Neither gate authenticates causal association. A completed-sender
variant is a lifecycle stress probe, not valid observed synchronous timing.

There are **6 base cases × 6 variants = 36 scenarios**, and **36 × 4 = 144
comparisons**. They require **41 distinct Jev calls**: 36 full-context requests
plus five HTTP requests (the opposite-role pair shares one). Both gate arms
reuse one of these responses, so they add no model calls. Six lifecycle versions
of one role pair are not six independent role families. Provider caching across
this run and earlier studies is unknown.

## Reproduce

Python 3.11+ and the standard library suffice. From this directory:

```sh
python3 -m unittest -q
python3 study.py --output "$HOME/nomos-context-pending-gate/a-fresh-run"
# Supply JEV_API_KEY privately in the environment; never write it into files.
python3 study.py --output "$HOME/nomos-context-pending-gate/a-fresh-run" --execute
```

Preparation freezes sources, exact requests, labels, gate inclusion decisions
and call order. Use a fresh directory. Execution is serial, stops on any failure,
and refuses to retry an attempted directory. Keep all failed/uncertain evidence.
Both questions are sent together in each call to `jev-1.14.0`, with an eight-second
timeout and the unchanged inclusive 0.70 threshold. Never tune after results.

Results are split by base pair and lifecycle variant. A comparison can reuse a
response; it is not an independent model observation. This test does not prove
that retained context is authentic, current, complete or the cause of the HTTP.
