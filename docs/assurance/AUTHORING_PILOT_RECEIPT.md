<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Evaluation authoring pilot receipt

Recorded October 27, 2026, using the source hashes below. This is a controlled
fixture and an offline task proof, not a real-agent quality measurement.
Reproduction commands and limits are in [EVALUATION.md](EVALUATION.md#evaluation-authoring-pilot).

Normal `CouncilService.propose_intent` orchestration produced three denials and
one approved correction, each with one deterministic fixture member review.
Assurance verified five signed records, detected repeated unchanged denial, and
used the operator preview/download APIs. The selected bundle contains only the
finding's supporting requests; the corrected intent is separate. The test secret
was absent from all archive members. The client-subject preview reported
`unsupported_for_trace_derivation`, without an invented user task or result.

The task uses the downloaded bundle plus explicitly labeled staging/missing-scope
fixture variations. No private ledger, signing key, model credential or unexported
correction is a task input. Proposed commands never execute. The source council
member and Harbor controls are deterministic test infrastructure.

| Toolchain | Pin |
| --- | --- |
| Eval Author | 20f95a0a010d4905c13065678af51970404910c9 |
| Harbor | 0.23.0 |
| Python | 3.13.10 |
| Task authoring route | Repository first-eval/manual; no council ATIF claim |

| Native Harbor control | Reward | Runtime exceptions | Pinned recorder status |
| --- | --- | --- | --- |
| reference | 1 | None | passed |
| nop | 0 | None | passed |
| incorrect-scope | 0 | None | passed |
| alternative | 1 | None | passed |

A separate, explicitly synthetic client-format fixture also passed Harbor's native
`Trajectory.model_validate_json` reader. It uses the exporter's narrow serializer,
including an explicit absence marker for an unrecorded agent version. This tests
format compatibility, not an observed client run or a proven client task. The
integration test separately runs Eval Author's pinned trace preparation command.

Native trial results and the pinned Eval Author receipts were retained privately.
These hashes identify those immutable artifacts; fresh reruns differ.

| Artifact | SHA-256 |
| --- | --- |
| Selected export ZIP | 844a16f57d9df9fa86a87ffbd51991d0b3c8140ced604bd67c8190fa04be6fd1 |
| Capture identity | 318bda98d8702ec8b7080c57409d21b9ff180fb66416d91e5b1b787d0d4c2c9f |
| Task revision manifest | 3f44617a91d53501a77c11bff49a92b43fdefcd832aa2129f04554bbda16a3fc |
| Synthetic native ATIF format fixture | 58f84de8783d35c85cc3f812bf7d4b68556042b39384b0ad64779c300b5891a8 |
| reference native result | 30d55441b5b9d873a161e2f8222b32cadf27a4b60d3b5dd5bef8b8c890d42576 |
| reference recorder receipt | 7418106d400db03c0935453ab613a1d9d187cbfc796fa8b588548ac627e9566c |
| nop native result | 21c36e5aeed203233f8459095dacfb83248107dcfe0c3fb9376424711c8381db |
| nop recorder receipt | c04c9bcc6043b6f379b8b1f769ddcdefd3c9a4e8bfb9393e9b0d8587687d9b80 |
| incorrect-scope native result | 047f35272ba8883d369dd223a00f1c92d8b80e0c9ec52ecfffee717706771fc1 |
| incorrect-scope recorder receipt | 1ca02dbe8d756846f0fe26620d2a7c41b362fc8cdf6df8001ce475b426c8546b |
| alternative native result | df0b1a523e78415e23629e7000c6e1b2d581ca78578555e46fd70f26ad651826 |
| alternative recorder receipt | ca88fa10bdcbff8c0a633717a523f5579a05f8085fdf44006513d2f1c4f3a27a |

Source used for this proof:

| File | SHA-256 |
| --- | --- |
| `assurance/app.py` | 8cbd76dfa2c87ff0a86e4e499a49dc5ab133b3efb1a439543dca1d40a4e8845e |
| `assurance/author_export.py` | eb0bbd023bee67c8a22e3e1c43d75530feb476937f04a2fc6161f1b2d1229f18 |
| `assurance/monitor.py` | 7fc0391b4ca320744f864e8adf11eec6203bd1f370d24a205ca09f90785b0d13 |
| `examples/assurance-evaluation/council-scope/README.md` | 495366fb3aab9b1ff6624ac0549370ddd359d01d892ad46e02c525461aa14987 |
| `examples/assurance-evaluation/council-scope/controls.py` | 07e1c41c8cce9d5ab9fd811634e26815658370ad5f7b1ef83e57562c86980272 |
| `examples/assurance-evaluation/council-scope/environment/Dockerfile` | a72babee9f366d3b42c895cc3f9555a828d7d36e9b35b619cfc7af7f87b16792 |
| `examples/assurance-evaluation/council-scope/instruction.md` | daedf5ae846a75394fdcf80eade0b78b7e2fd5a81f9fef0e2207ff92a03a82e5 |
| `examples/assurance-evaluation/council-scope/solution/solve.sh` | c66e559f70086545d4dcb963d6c90eabfc643b83381f222bc86a2a385c116e08 |
| `examples/assurance-evaluation/council-scope/task.toml` | d3c49f75c8b64fe515d9f6cde306028e303f92ba1e00fffab34dcc0f1d956f15 |
| `examples/assurance-evaluation/council-scope/tests/test.sh` | 7b339a2858aeb3674cab9460f05baf66dcd3bc0faaab14e51d4d48282ede0c06 |
| `examples/assurance-evaluation/council-scope/tests/verify.py` | 0c59d884c8af42d26c27700679e9df21141e5601fd8ff1f85ddf044de8a9e3e1 |
| `scripts/assurance_authoring_example.py` | cfe6fee88872d99656083436fed12c1f65242219dd8ff504ebfb4a6cd57873eb |

Actual council/client performance, audit coverage closure and optimization remain
unmeasured. The pilot does not execute a client command or access production.
