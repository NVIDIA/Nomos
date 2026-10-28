<!-- SPDX-License-Identifier: Apache-2.0 -->
# Reviewed controlled pilot suite

This historical suite produced the [rejected pilot](../../../../docs/assurance/OPTIMIZATION_PILOT_REPORT.md)
on October 28, 2026. It contains four controlled cases, three repetitions per arm,
the reviewed contract, baseline and unchanged candidate instruction, source mapping,
proof summary and runtime identity. It is not a production capture or benchmark.
The final family is exposed; retire it before any candidate edit informed by results.

`source-authoring-bundle.zip` is the sanitized controlled rich export.
`review-notes.json` records its SHA-256 and label/contract review.
`reviewed-experiment.json` is the sanitized reviewed definition, not a frozen
experiment directory. The [receipt](result.json) preserves
measured identities and gates. Original private trials, failed attempts and signed
runtime evidence remain outside the repository.

For offline intake, run from the repository root:

```sh
python -m evals.nomos intake evals/nomos/suites/pilot-20261028/source-authoring-bundle.zip .runs/nomos-optimization/reproduced-intake
```

Use the [command guide](../../README.md) and its pinned prerequisites. Build
`evals/nomos/Dockerfile`, record the resulting immutable image ID, and copy the
reviewed definition privately. Update its runtime identity and rerun native
controls against that image before freezing a new experiment. A new runtime or
implementation creates a new experiment; do not claim its hashes match this
historical run. Native controls require no provider credential:

```sh
python scripts/assurance_optimization_controls.py --spec PRIVATE_SPEC --out NEW_CONTROLS_DIR --harbor-python HARBOR_PYTHON --image IMMUTABLE_IMAGE_ID
```

Fresh provider-backed staged reproduction uses `assurance_optimization_pilot.py`
with `--case-count 4`, a private provider configuration and a fresh bounded budget.
Never resume the exhausted historical budget. A new measurement can differ because
the remote provider does not expose immutable model weights. Preserve missing
client context and authored supplements as declared by the bundle and contract.
