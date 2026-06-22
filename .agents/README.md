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

# Agent Guidance

This directory is the canonical home for AI-facing repository guidance.
Human-facing product and operator docs belong in `docs/`; durable rules for
coding agents belong here.

Start here:

- `architecture.md` - physical design, levelization, and John Lakos-style dependency rules.
- `security.md` - security posture, auth, OPA, secrets, and privileged execution expectations.
- `coding-guidelines.md` - implementation standards, TDD, unit tests, and review discipline.
- `deployment.md` - Docker stack, env files, accounts, and release checks.
- `troubleshooting.md` - known failure modes and fast diagnosis paths.

Agents working in this repository should treat these files as project
constraints. If the codebase and these files disagree, either update the file
as part of the change or call out the mismatch explicitly.
