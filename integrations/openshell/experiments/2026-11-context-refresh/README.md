<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Revised compact-context benchmark

This rerun compares the latest formatter and combined evidence scope across
the existing 36 role-study cases and 96 distraction cases. The
[reviewed plan](PLAN.md) defines the four conditions, including a Council-only
ablation. Earlier benchmark inputs, results and reports remain unchanged.

## Current status: model assessment blocked by connectivity

On 4 November 2026, preparation produced **132 cases, 528 logical comparisons
and 516 distinct model requests**. Offline tests passed input invariants,
deduplication, and durable fail-stop behavior. The first live request returned
`transport_error` before receiving HTTP; the runner stopped after that one
attempt. There are **zero successful new Jev assessments**. This is not a deny,
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
Do not rerun that output directory. No model-quality conclusion or new
question candidate is warranted until connectivity is restored.

## Reproduce after resolving connectivity

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
