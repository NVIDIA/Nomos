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

# Coding Guidelines

## Karpathy-Inspired Agent Discipline

These rules adapt Andrej Karpathy's public guidance on avoiding common LLM
coding-agent failure modes. Apply them on every code change.

### Think Before Coding

Do not silently fill in missing requirements.

- State assumptions when they matter.
- Ask when multiple interpretations would lead to different code.
- Surface simpler alternatives or risky tradeoffs before implementing.
- Stop and clarify if the requested behavior, ownership boundary, or success
  condition is ambiguous.

### Simplicity First

Write the smallest code that solves the current problem.

- Do not add speculative features, options, framework layers, or generality.
- Do not introduce an abstraction for one call site.
- Prefer boring, readable control flow over cleverness.
- If a solution grows larger than the problem deserves, simplify before
  committing it.

### Surgical Changes

Every changed line should trace to the requested outcome.

- Match the surrounding code style even when you would design it differently.
- Do not refactor adjacent code unless it is required for the change.
- Remove only dead imports, variables, functions, or files made obsolete by
  your own change.
- Mention unrelated cleanup opportunities instead of mixing them into the
  commit.

### Goal-Driven Execution

Turn tasks into verifiable outcomes and loop until they are checked.

- Define what success means before editing.
- Add a failing test first when behavior changes.
- Run the narrowest relevant verification while iterating.
- Run the broader suite or documented validation command before handoff when
  the change can affect shared behavior.
- If verification cannot run locally, record the blocker and residual risk.

## Test-Driven Development

TDD and unit testing are mandatory for meaningful behavior changes.

Expected workflow:

1. Add or update a focused failing test that describes the desired behavior.
2. Implement the smallest production change that makes the test pass.
3. Refactor only after the test is green.
4. Run the relevant focused tests.
5. Run the full suite before commit or handoff.

Bug fixes should include a regression test unless the fix is purely mechanical
or impossible to test without external infrastructure. If no test is added,
state why.

## Coverage

Coverage is enforced by pre-commit. The current floor is intentionally set to
the repository's measured baseline and should only move upward unless there is
a documented reason.

When adding code:

- Add unit coverage near the changed component.
- Cover failure paths, not only happy paths.
- Prefer deterministic tests over live-service tests.
- Use live smoke tests only for deployment and transport verification.

## Implementation Style

- Follow existing local patterns before creating new abstractions.
- Use Pydantic models or typed dataclasses for structured data crossing
  boundaries.
- Keep public contracts stable and small.
- Avoid broad refactors in feature commits.
- Do not introduce cross-level imports to save a few lines.
- Prefer explicit error messages that help operators fix configuration.

## Comments and Docs

Write comments for non-obvious policy, security, or protocol reasoning.
Do not narrate obvious code. Update docs when behavior, deployment, or public
interfaces change.
