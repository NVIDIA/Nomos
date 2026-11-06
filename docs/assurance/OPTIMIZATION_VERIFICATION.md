<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Walkthrough execution evidence

On November 4, 2026, the explicit `python -m evals.nomos run --executor harbor`
path completed six native Harbor 0.23.0 trials using Python 3.13.10 and Docker
29.0.1 on macOS. The worker ran real isolated council orchestration and signed
ledger collection, against a local HTTP endpoint returning a fixed approval.
No external inference endpoint was used and no proposed command executed.

The experiment used two development cases (permitted staging and forbidden
production), three repetitions each, baseline only. Three approvals correctly
passed and three approvals correctly failed. All six native results had no
exception. The CLI reported six expected/completed trials, zero errors/missing,
and no stop reason; the shared budget retained six reservations. Completion
therefore did not conceal wrong-answer scores.

Sanitized receipt:

```json
{
  "subject": "CLI native Harbor wiring only; fixed local responses, not model performance or agent authoring",
  "runtime_image": "sha256:54281a60443be839a01ddabfbad7cc534b6c9085c6db8e23fd103326dc9c0d09",
  "completed": 6,
  "independent_passes": 3,
  "native_rewards": [
    1.0,
    1.0,
    1.0,
    0.0,
    0.0,
    0.0
  ],
  "shared_reservations": 6,
  "external_model_calls": 0
}
```

CLI source SHA-256: `9c8d4ca4d5217284bc7f6468b5cf4ee8cccda361c4c46cd7c43c104446a12475`. <!-- pragma: allowlist secret -->

The private output root was `/private/tmp/nomos-cli-proof-20261104-fixed`, with
`receipt.json`, `reviewed.json`, `budget.json`, the frozen experiment, attempts,
responses/interactions, native results and verified ledger evidence. These
artifacts are local retained evidence, not repository-distributed inputs.

The first attempt stopped after two infrastructure errors because the initial
CLI resolved a virtual-environment interpreter symlink to the base interpreter,
which lacked Harbor. Its reservations and logs remain at
`/private/tmp/nomos-cli-proof-20261104`; they were not erased or refunded. The
corrected CLI preserves the venv path, its regression test uses an interpreter
symlink, and the successful measurement used a new experiment/budget.

This verifies CLI-to-Harbor wiring, independent wrong-answer scoring, correlation
and accounting. It does **not** establish that a coding agent authored new tests
or an effective candidate from a dashboard download, nor evaluate a real model,
production policy or client recovery. That full live walkthrough remains an
operator exercise with its own evidence and approved provider budget. The
[historical model-backed pilot](OPTIMIZATION_PILOT_REPORT.md) is separate evidence.

`tests/evals/test_cli.py` covers direct/default compatibility, explicit Harbor
selection, invalid-option/image checks before reservations, incomplete execution
exit status, resume without redispatch, shared diagnosis accounting and budget
reset refusal. The review-loop session additionally checks the merged assurance
and evaluation suites plus native authoring and council wiring controls; its
private report records the exact candidate and target and any CI-only gaps.
