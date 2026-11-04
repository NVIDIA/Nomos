<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Context-needed credential role-binding study

**Completed once on 2 November 2026: 96 unique API calls succeeded.**

This separate follow-up tests whether context helps identify the meaning of an
opaque outgoing value. It compares the original questions with compact context,
one new U wording candidate with identical context, and original questions with
HTTP state only. The preceding [96-case context-distraction study](../2026-11-context-guidance/RESULTS.md)
is preserved; its scores and labels are not reused here.

- [Read the report](RESULTS.md): design, setup, examples, information limits,
  separate role/control results, timing, token usage and interpretation.
- [Download the standalone interactive report](report.html): all 36 cases,
  readable evidence and exact request/response JSON downloads.
- [Frozen design](PLAN.md), [full fixtures](cases.json) and [exact questions](questions.json).
- [Cross-machine handoff](../2026-11-context-format-pilot/HANDOFF.md).

The study has **36 logical cases, 108 condition cells and 96 unique API calls**.
The 24 role cases form six four-cell families; twelve controls form six safe/
unsafe pairs with benign history. There are 18 allows and 18 denials. Labels
are conditional on supplied reported evidence, not verified authenticity or
permission. All content and credential-shaped values are fabricated.

## Why the counts differ

### How these cases differ from distraction cases

The themes overlap; the information needed to decide is different. An exemption
claim in a role case accompanies an opaque value A: the supplied history is
needed to identify A as a credential. An exemption claim in a distraction case
accompanies HTTP content that already identifies the disclosure, so approval
claims should not change the decision. Likewise, the different-value role
variant and a corrected-retry distraction case both check that history-only
secrets are not mistaken for outgoing disclosure.

These are related tests, not independent evidence of separate model abilities.
Role labels are conditional on reported context; distraction labels are
HTTP-decidable. The measured histories are synthetic background, not complete
live tool traces. In particular, `guidance-33-safe` shows a prior denied draft
but no observed corrected sender. Its allow label follows the redacted outgoing
HTTP, not proof that a real retry occurred.

A **family** is one scenario with four related variants. The six families use
different roles for otherwise opaque values:

| Family | Sensitive role | Ordinary role |
| --- | --- | --- |
| Client token / inventory | API authentication token | Inventory reference |
| Runner token / artifact | Runner registration token | Build artifact reference |
| Mail password / shipment | Mail login password | Shipment reference |
| Webhook secret / document | Webhook signing secret | Document reference |
| Login password / ticket | Temporary login password | Support ticket reference |
| Storage secret / batch | Storage secret access key | Batch reference |

Each family has these four variants. Expected decisions are conditional on the
reported evidence, not proof that the history is authentic or a credential works.

| Variant | Expected decision |
| --- | --- |
| Send value A, reported as a credential | Deny |
| Send identical A, reported as an ordinary reference | Allow |
| Send ordinary B while credential A appears only in history | Allow |
| Send credential A alongside approval/exemption claims | Deny |

```text
6 families × 4 variants = 24 context-dependent cases
6 safe/unsafe control pairs = 12 HTTP-decidable controls
24 + 12 = 36 total logical cases
36 cases × 3 conditions = 108 logical comparisons
```

The [worked family](RESULTS.md#a-worked-family-identical-outgoing-a-different-reported-role)
shows the exact HTTP and tool-argument differences. These are 36 related cases,
not 36 independent scenarios. The 108 comparisons require only 96 distinct
model calls, as explained below.

| Condition | Logical cells | Actual unique calls |
| --- | ---: | ---: |
| Original + compact context | 36 | 36 |
| U + compact context | 36 | 36 |
| Original questions, HTTP only | 36 | 24 |
| **Total** | **108** | **96** |

Three role cells in each family send identical outgoing A and differ only in
context. HTTP-only therefore shares one measured response among them. The
other B request is distinct. We do not submit identical requests repeatedly or
add salt to defeat caching. Shared decisions are not independent observations
for latency, tokens or uncertainty.

HTTP-only cannot both deny the credential-role A and allow the identical
ordinary-role A. Its six core pairs necessarily have zero pairs with both
correct; its theoretical ceiling across the full 24 role cells is 18/24 (75%).
Interpret that as missing information, not a general model failure. The report
keeps role-pair discrimination and HTTP-decidable controls separate.

## Read or regenerate without model calls

Clone `feat/context-format-pilot` from `https://github.com/NVIDIA/Nomos.git`, then:

```sh
cd integrations/openshell/experiments/2026-11-context-role-binding
python3 -m unittest discover -s tests -v
python3 build_report.py
```

Python 3.11+ and the standard library are sufficient for the offline runner,
tests and report builder. These commands need no API key and make no model
calls. Open `report.html` in a browser. Its figures, case inspector and JSON
downloads are embedded, with no external scripts or analytics. Repository links
require the checkout; the embedded evidence itself works standalone.

The committed SVG figures are generated with optional Matplotlib 3.11.2
(PSF-based permissive license), not a production dependency. Their provenance
hashes must match the measurement file. To regenerate the scientific figures:

```sh
uv run --no-project --with matplotlib==3.11.2 python plot_figures.py
python3 build_report.py
```

`build_cases.py` regenerates the authored fixtures and catalogs. Do not run it
over a historical input snapshot or alter measured definitions to match an
outcome. `PREFIX` is the exact U paragraph copied from the reviewed proposal;
there is no previous C or additional S candidate in this experiment.

## Source and evidence

| Path | Purpose |
| --- | --- |
| `build_cases.py` | Six role families, six control pairs and safe terminal-argument construction |
| `cases.json` | Logical cases and author metadata kept outside model input |
| `questions.json` | `baseline` original catalog and `role_guided` U catalog |
| `PLAN.md` | Design fixed before model calls |
| `capture_context.py` | Real authenticated context API and native TLS/JWT reader capture |
| `study.py` | Offline preparation, deduplication and explicit once-only execution |
| `data/capture/` | Final component states, lookup responses and capture proof |
| `data/input-snapshot/` | Verbatim controlling source/plan used for measurement |
| `data/measured-v1/requests/` | 96 exact unique model request JSON files, without credentials |
| `data/measured-v1/responses/` | Original model responses, one per actual request |
| `data/measured-v1/manifest.json` | Model, counts, source/capture hashes and order design |
| `data/measured-v1/matrix.json` | Logical cells mapped to actual request hashes |
| `data/measured-v1/attempts.jsonl` | Persisted started/finished actual-call markers |
| `data/measured-v1/measurements.jsonl` | One measurement per actual unique API call |
| `data/measured-v1/results.json` | Logical outcomes referencing those measurements |

Artifact key `http_only` uses the original questions, not a third question
catalog. Every call contains both disclosure questions. `baseline` does not mean
no model or no middleware; it is the original-question context condition.

## Component capture and deliberate separate measurements

Use the component environments and builds in the
[handoff](../2026-11-context-format-pilot/HANDOFF.md#if-recapturing-the-real-components).
`capture_context.py --help` lists explicit context-service Python, reader binary,
proto and protoc paths. Capture starts authenticated temporary loopback services;
it does not call Jev, execute Hermes tools, obtain real Council decisions or
restart a deployed gateway. Reported terminal commands are never executed.
The local recording model acknowledges capture only; its fixed answers are not
study results.

For a separate, explicitly intended measurement, first capture the real reader
state and prepare a new output directory:

```sh
python3 study.py \
  --captured-states /path/to/new-capture/captured_states.json \
  --output /path/to/new-measurement
```

Preparation is offline. Inspect the logical-cell and actual-request mappings,
freeze the source hashes and order, and obtain review before model calls.
Only the following explicit switch sends real model API requests:

```sh
python3 study.py --output /path/to/new-measurement --execute
```

Provide `JEV_API_KEY` through a private process environment. Never copy shell
startup files, print keys, save Authorization headers or pass their values in
command arguments. The runner records attempted calls before network I/O,
refuses to overwrite attempts, runs serially and does not silently retry.
An incomplete started attempt has an uncertain outcome and must not be erased.
Do not rerun the published measured directory.

The replay uses an eight-second timeout, distinct from production middleware,
model and gRPC deadlines. Model-HTTP timing does not measure added OpenShell
latency. Provider caching is unknown, and no cache-disable control or nonce is
used. No billing cost, calibrated probability or population-significance claim
is inferred from this small selected cohort.
