<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: LicenseRef-NvidiaProprietary

NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
property and proprietary rights in and to this material, related
documentation and any modifications thereto. Any use, reproduction,
disclosure or distribution of this material and related documentation
without an express license agreement from NVIDIA CORPORATION or
its affiliates is strictly prohibited.
-->

# Architecture Guidelines

Nomos is a layered system. Physical layout is part of the design, not
an afterthought. Optimize for correctness, reviewability, and AI readability:
future agents should be able to infer ownership, dependency direction, and
blast radius from paths and imports.

## John Lakos Principles

Use John Lakos-style large-scale C++ design principles even though this is a
Python repository:

- Physical dependencies must be intentional, minimal, and visible.
- Dependency direction must follow levelization. Lower-level packages must not
  import higher-level packages.
- Prefer acyclic dependency graphs. If two modules need each other, extract a
  lower-level abstraction instead of creating a cycle.
- Keep stable abstractions in lower levels and volatile orchestration in higher
  levels.
- Package boundaries should correspond to design boundaries. Do not hide a
  cross-level dependency behind convenience imports.
- Localize change. A narrow feature should have a narrow physical footprint.
- Avoid utility junk drawers. Shared code belongs at the lowest level that
  honestly owns the abstraction.

For Python, treat packages and modules like physical components. Imports are
dependencies. Public constructors, Pydantic models, protocol objects, and
contract modules are component interfaces.

## Level Rules

Current architectural intent:

- `nomos/level_0`: primitives, types, clocks, crypto. No dependencies
  on policy, quorum, executor, council, NAT, MCP, Redis, or LLM code.
- `nomos/level_1`: policy and ledger foundations. Depends on Level 0.
- `nomos/level_2`: capability and quorum services. Depends on lower levels.
- `nomos/level_3`: verification and execution support. Depends on lower levels.
- `nomos/level_4`: council orchestration. Depends on lower levels, not
  transport adapters.
- `nomos/nat`, `nomos/mcp`, `nomos/grpc`: ingress and
  adapter layers. These may depend inward, but domain layers must not depend
  on them.
- `trusted_executor_contract`: public contract surface for third-party
  executors. Keep it small, stable, and transport-neutral where possible.
- `executors`: trusted executor implementations. `executors/docker` is the
  local subprocess reference and `executors/openshell` adapts to OpenShell;
  both must demonstrate the contract without becoming the contract.
- `demo`: demo UI and proxy. Demo code must not leak into core packages.

## AI Readability

Code should be easy for an agent to navigate safely:

- Use explicit names over clever indirection.
- Keep files cohesive and small enough that their purpose is obvious.
- Put protocol and schema definitions near the boundary they govern.
- Prefer structured data models over ad hoc dict/string protocols.
- Preserve existing patterns unless there is a concrete architectural reason
  to change them.
- Update docs and tests in the same change when architecture shifts.

## New Module Checklist

Before adding a module or package:

- Identify its level and owner.
- Confirm imports only point downward or sideways within the same component.
- Add unit tests at the same architectural level.
- Add integration tests only when crossing boundaries is part of the behavior.
- Document any new public contract in `docs/` and any new agent convention here.
