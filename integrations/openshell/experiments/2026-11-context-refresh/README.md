<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Revised compact-context benchmark

This rerun compares the latest formatter and combined evidence scope across
the existing 36 role-study cases and 96 distraction cases. The
[reviewed plan](PLAN.md) defines the four conditions, including a Council-only
ablation. Earlier benchmark inputs, results and reports remain unchanged.

## Completed comparison

On 4 November 2026, a fresh run from `a944783` completed all **516 distinct
Jev calls**, yielding **528 comparisons over 132 cases**, without retries or
threshold tuning. Read [RESULTS.md](RESULTS.md) for findings, error denominators,
all regressions, limitations, and the independent subagent audit.
Exact inputs, raw responses and attempt markers are retained in
[data/measured-run-001](data/measured-run-001/).

Original + current compact context and Original without Council each passed
24/24 role cases and 6/6 core pairs. U passed 19/24 and 3/6, introducing five
harmful allows. On the separate 96 distraction cases, Original had three false
blocks and one harmful allow; U one/four; without Council two/one; HTTP-only
one/zero. Council effects were mixed, with no benefit established here.
The controls and HTTP-only's role-information limit are reported separately;
do not pool all cases into an overall context recommendation.

Offline provenance and paired analysis can be regenerated without new calls:

```sh
python3 analyze.py --output data/measured-run-001
```

The raw evidence copy was independently checked against the original external
run. No production formatter, question, threshold or deployment changed.

## Earlier attempt: model assessment blocked by connectivity

On 4 November 2026, preparation produced **132 cases, 528 logical comparisons
and 516 distinct model requests**. Offline tests passed input invariants,
deduplication, and durable fail-stop behavior. The first live request returned
`transport_error` before receiving HTTP; the runner stopped after that one
attempt. That attempt had **zero successful Jev assessments**. This is not a deny,
a harmful allow, or evidence that context helps or hurts.

Subsequent credential-free diagnostics found connection resets during TLS to
`api.typesafe.ai` and `docs.typesafe.ai`; both resolved IPv4 API routes failed,
and IPv6 was unavailable. Default and explicit HTTP/1.1/TLS-1.2 curl probes also
failed, including with environment proxies bypassed. GitHub TLS succeeded.
The public TypeSafe documentation still lists `jev-1.14.0` and the same
`https://api.typesafe.ai/v1/systemone` endpoint. These observations do not
identify the cause or establish a global TypeSafe outage. TLS verification was
not disabled and no alternate model was substituted.

The failed run and frozen sources remain in the initiating workspace under
`data/measured-v1/`; they are not included as new benchmark results in this PR.
Do not rerun that output directory. No model-quality conclusion or new question
candidate was warranted from that failed attempt. The successful fresh run above
is separate evidence; restored connectivity does not identify the earlier cause.

## Reproduce in a fresh output directory

Python 3.11+ and the standard library suffice. From this directory:

```sh
python3 -m unittest -q
# Use a fresh directory outside the checkout, never a historical measured run.
python3 benchmark.py --output /path/to/new-context-refresh
# Supply JEV_API_KEY through the environment or your secret mechanism first.
# Never put its value in commands, reports or request artifacts.
python3 benchmark.py --output /path/to/new-context-refresh --execute
```

Preparation makes no network model calls. It writes exact requests, source
snapshots/hashes, a logical matrix and a globally deduplicated call order.
Logical arm positions are near-balanced within sorted cohort/label strata;
actual calls follow first occurrence after deduplication, so the full call
order is not globally randomized or guaranteed balanced after sharing.
Execute serially without retries. A failed or uncertain request stops the run
and returns a nonzero process status, retaining attempted evidence.

Once completed, regenerate the JSON summary without another model call:

```sh
python3 benchmark.py --output /path/to/new-context-refresh --summarize
```

Read `results.json` separately for 24 role cases, 12 role-study controls and
96 distraction cases. Compare `original_context` against `guided_context`
to assess U wording, against `without_council` to isolate reported Council
content, and against `http_only` to assess all retained context. Inspect false
blocks, harmful allows, incomplete assessments and both-correct role pairs;
do not pool the intentionally underinformed HTTP-only role labels into a
general accuracy claim.

The captured histories are synthetic, timestamp-free historical fixtures.
This rerun tests the new presentation and question interaction, not live tool
execution, real Council decisions, timestamp usefulness, or actual egress.
Further guided candidates must be frozen in a separate run and identified as
exploratory development on already-inspected fixtures, not independent proof.
