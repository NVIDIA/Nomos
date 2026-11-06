<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Council scope authoring pilot

This reviewed task template follows Eval Author's repository-based first-eval
workflow at `20f95a0a010d4905c13065678af51970404910c9`. It is not a native
council trace import or a measured audit-gap task. See the exact capture,
preparation, and Harbor 0.23.0 rerun commands in
[the evaluation guide](../../../docs/assurance/EVALUATION.md).

Run the Nomos pilot script to create the missing evidence and request fixtures
from an operator-authenticated downloaded bundle. The task consumes that download
and named fixture variations; it does not read the pilot's private ledger/keys.

The case inventory is three recorded production requests, a proposed staging
correction, and a missing-scope negative. The latter two are external test-design
supplements. Expected decisions come from the exported fixture contract plus the
instruction's missing-scope rule. Source decisions are not grading authority.

The environment is offline Python with JSON files, with no dependency on live
council/model services. Its build excludes the solution and verifier. Harbor
uploads those only in the corresponding control/grading phase. Verifier-owned
requests keep grading independent of agent edits to visible input. Oracle and
the alternative output format must pass; NOP and approve-everything must fail
without runtime exceptions. There is no live side effect to validate.

These controls prove this specific task/verifier path. They do not measure a
real council agent, demonstrate optimization, or establish general client support.
