<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Short context-guidance experiment

**Completed once, 2 November 2026: 48 new cases, 96 actual Jev calls.**

- [Read the results](RESULTS.md): design, terminology, worked examples, decisions,
  timing, token usage, interpretation and limitations.
- [Open/download the standalone HTML report](report.html): the same explanation,
  embedded charts and all 48 cases with readable model evidence.
- [Agreed test plan](PLAN.md) and [cross-machine handoff](../2026-11-context-format-pilot/HANDOFF.md).

The original questions (Control) and a short context-guidance prefix (C) each
matched 44/48 labels. C reduced false blocks but increased harmful escapes.
The original questions remain the recommendation; nothing was deployed.

## Inspect or regenerate offline

Clone `feat/context-format-pilot` from `https://github.com/NVIDIA/Nomos.git`, then:

```sh
cd integrations/openshell/experiments/2026-11-context-guidance
python3 -m unittest discover -s tests -v
python3 build_report.py
```

These commands use Python 3.11+ and the standard library. They make no model
calls. Open `report.html` in a browser; graphs and decoded evidence are embedded,
with no external scripts, fonts or analytics. Relative repository links require
the checked-out folder; the report text and case inspector work standalone.

## What is preserved

| Path | Contents |
| --- | --- |
| `cases.json` | 48 cases, categories, frozen overall/per-rule labels and rationales |
| `questions.json` | Exact original and prefix-added question catalogs |
| `build_cases.py` | Reproducible fictional content generator |
| `study.py` | Offline preparation and explicit once-only model execution |
| `capture_context.py` | Actual HTTPS context API and native TLS/JWT reader capture |
| `data/capture/` | Final captured states, lookup responses and component proof |
| `data/measured-v1/requests/` | All 96 exact model requests without authentication headers |
| `data/measured-v1/responses/` | All 96 original model responses |
| `data/measured-v1/manifest.json` | Model, count, seed, source/capture hashes and timing settings |
| `data/measured-v1/matrix.json` | Frozen call order and labels; labels are not model input |
| `data/measured-v1/attempts.jsonl` | Started/finished markers persisted around every network call |
| `data/measured-v1/measurements.jsonl` | One measurement per actual API call |
| `data/measured-v1/results.json` | Formatted decisions with linked measurements |
| `data/input-snapshot/` | Verbatim measured source files; never overwrite them |

`baseline` in artifacts means Control; `context_aware` means C. These names are
retained for compatibility with the shared pilot helper, not three conditions.
The shared transport/formatting helper is the sibling format pilot's
`run_pilot.py`, identified in the manifest's source hashes.

## Reproduce component capture or deliberately collect a new run

Do not rerun the measured folder: one call per case/condition is the published
study. Use a fresh directory, new manifest and separately identified experiment
for any later measurement. Never discard an attempt to improve an outcome.

For real component recapture, use the environments/builds described in the
[handoff](../2026-11-context-format-pilot/HANDOFF.md#if-recapturing-the-real-components)
and run this folder's `capture_context.py --help`. It creates isolated temporary
loopback services and credentials; no model calls. This version excludes both
worker tool-definition bookkeeping fields and publishes earlier snapshot parents
before their events. The first preparatory capture was replaced by a second
capture after replacing a key-shaped fixture with an SMTP-password fixture;
no Jev measurements were made using that first capture. Neither capture ran
Hermes or Council, and no private deployment credentials are preserved.

To prepare a separate, explicitly intended new measurement directory:

```sh
python3 study.py \
  --captured-states /path/to/new-capture/captured_states.json \
  --output /path/to/new-measurement
```

Only the following explicit command sends real model API calls. Obtain `JEV_API_KEY`
privately in the process environment; never save it in this repository or pass
its value as a command argument.

```sh
python3 study.py --output /path/to/new-measurement --execute
```

The runner verifies frozen source/request hashes, records each start before
network I/O, runs serially, stops at the first failure, and refuses to rerun an
attempted directory. A start without a finish means the outcome is uncertain;
consult the operator rather than silently retrying it. Latency is model-HTTP
round trip with an eight-second diagnostic timeout, not production middleware
or sandbox latency. Caching is unknown. No API key is needed to inspect the
completed evidence or rebuild the report.
