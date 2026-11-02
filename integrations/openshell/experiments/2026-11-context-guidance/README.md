<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Expanded context experiment: three conditions on 96 cases

**Completed once, 2 November 2026: 96 cases and 288 newly collected Jev calls.**

- [Read the report](RESULTS.md): design, setup, terminology, worked examples,
  three-condition decisions, paired changes, category/cohort results and limitations.
- [Download the standalone interactive report](report.html): embedded figures,
  readable evidence and request/response JSON downloads for every condition.
- [Post-experiment wording and coverage review](FOLLOW_UP_REVIEW.md): untested
  suggestions and a focused context-needed follow-up; no additional model calls.
- [Frozen design](PLAN.md) and [cross-machine handoff](../2026-11-context-format-pilot/HANDOFF.md).

| Condition | Correct against author labels | Benign false blocks | Harmful escapes |
| --- | --- | --- | --- |
| Original questions + compact context | 85/96 | 10/48 | 1/48 |
| Short guidance C + compact context | 83/96 | 2/48 | 11/48 |
| Original questions, HTTP only | 94/96 | 1/48 | 1/48 |

HTTP-only made fewer mistakes on this set, whose labels are all determinable
from outgoing content. This is not a general finding that context has no value.
C did not improve the comparison; nothing was deployed. Every condition still
uses Jev—HTTP-only does not mean raw OpenShell or unreviewed forwarding.

## Inspect or regenerate offline

Clone `feat/context-format-pilot` from `https://github.com/NVIDIA/Nomos.git`, then:

```sh
cd integrations/openshell/experiments/2026-11-context-guidance
python3 -m unittest discover -s tests -v
python3 build_report.py
```

Python 3.11+ and the standard library are enough. These commands make no model
calls and need no API key. Open `report.html` in a browser. All figures, evidence,
filters and JSON downloads work without external scripts or network access.
Ordinary repository links require the folder checkout; they are not needed to
read the embedded report or inspect/download the model inputs and responses.

By default, the inspector shows only cases with at least one incorrect decision.
Uncheck that option to browse all 96; category and existing/new filters can be
combined. Each case has three assessments, and all downloads preserve the
captured JSON values. Green means agreement with the author label; red means a
mismatch. The same request may have different outcomes in different conditions.

## Evidence and source layout

| Path | Contents |
| --- | --- |
| `cases.json` | Current 96 cases, categories, overall/per-question labels and rationales |
| `additional_cases.py` | 24 new scenario families, each with safe/unsafe siblings |
| `build_cases.py` | Original families and the reproducible 96-case generator |
| `questions.json` | Exact original and C catalogs; HTTP-only uses the original |
| `study.py` | Three-condition preparation, frozen order and explicit once-only execution |
| `capture_context.py` | Real HTTPS context API and native TLS/JWT reader capture |
| `report-template.md`, `build_report.py` | Human narrative and deterministic results/report generation |
| `report.css`, `report.js` | Embedded styling and offline filters/downloads |
| `data/capture-v2/` | Final 96-case component states, lookup responses and proof |
| `data/input-snapshot-v2/` | Exact sources and plan used for the expanded measurement |
| `data/measured-v2/requests/` | All 288 complete model requests, without Authorization headers |
| `data/measured-v2/responses/` | All 288 original model responses |
| `data/measured-v2/manifest.json` | Count, model, source/capture hashes, order design and timing settings |
| `data/measured-v2/matrix.json` | Exact frozen call order and labels outside model input |
| `data/measured-v2/attempts.jsonl` | 288 started and 288 finished markers |
| `data/measured-v2/measurements.jsonl` | One record per actual API assessment |
| `data/measured-v2/results.json` | Readable decisions linked to measurements |
| `data/measured-v1/`, `data/input-snapshot/`, `data/capture/` | Preserved previous 48-case/two-condition evidence, not pooled into current results |

Artifact names: `baseline` means Original + compact context; `context_aware`
means C + compact context; `http_only` means Original questions without context.
There are exactly three conditions. Model questions are not separate API calls:
96 cases x three conditions = 288 calls, each asking both disclosure questions.

## What was changed and what was preserved

The original 48 case definitions and labels are unchanged; only a cohort tag
was added outside model input. The new 48 add materially different examples
within the same six categories. All terminal commands are synthetic records,
not executed commands. Review caught incorrect shell quoting for apostrophes in
some new records; it was corrected and tested before collection, without
changing original cases. Preparatory captures are not model trials; only the
final capture was used to prepare the measured requests.

Each condition was assessed again for all 96 cases. Prior scores were not reused.
All 96 context-bearing requests for the original 48 exactly match earlier
request hashes. They were called again, but provider cache hits are unknown;
fresh collection therefore does not mean cold-cache timing. New content and prior content are reported separately.
No automatic retries, salts, undocumented cache controls, threshold changes or
post-result relabeling were used. The model remained `jev-1.14.0` throughout.
The short prefix is unchanged from the prior experiment.

## Deliberately recapturing components or collecting a separate run

Do not rerun or overwrite `data/measured-v2`. Published runs are immutable.
For a new, explicitly intended experiment, use new output directories and keep
all attempted calls—even failures or interrupted outcomes.

Builds and component environments are described in the
[handoff](../2026-11-context-format-pilot/HANDOFF.md#if-recapturing-the-real-components).
Run `capture_context.py --help` for its explicit service Python, reader binary,
proto and protoc paths. Capture starts temporary authenticated loopback services;
it does not execute Hermes, call Jev or restart an existing gateway.

Prepare a separate run offline:

```sh
python3 study.py \
  --captured-states /path/to/new-capture/captured_states.json \
  --output /path/to/new-measurement
```

Only this explicit switch makes real model API calls. Supply `JEV_API_KEY` through
a private process environment; never put its value in a command or repository:

```sh
python3 study.py --output /path/to/new-measurement --execute
```

The runner verifies source/request hashes, persists an attempt-start marker
before each network call, runs serially and stops at the first failure. It
refuses to rerun an attempted directory. A start without a finish is uncertain;
consult the operator rather than silently retrying. The replay uses an
eight-second timeout; its successful 6.17-second call does not validate a shorter
production middleware/model/gRPC deadline. Provider caching remains unknown.
No model calls are needed to inspect evidence or rebuild the report.
