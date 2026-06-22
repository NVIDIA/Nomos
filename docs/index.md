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

# Nomos Documentation

**Security Council + Writ of Execution + Sandboxed Execution**

Nomos is an authorization kernel for privileged operations with cryptographic accountability. Agents propose intent through monitored MCP or gRPC; OPA policy filters deterministic cases before LLM review; an LLM-backed council evaluates approved candidates; approved actions are sealed in a signed Writ of Execution and run inside an isolated container sandbox.

---

## Start Here

| Need | Start With |
|------|------------|
| Fastest copy/paste startup | [README Start Here](../README.md#start-here) |
| Guided first-run walkthrough | [Getting Started](interactive/getting-started.html) |
| Prove the deployed council works over MCP | [Council MCP Validation](council-mcp-validation.md) |
| Understand the whole request-to-execution path | [Workflow Walkthrough](interactive/workflow.html) |
| Deploy or operate the stack | [Deployment Setup](deployment-setup.md) / [interactive planner](interactive/deployment.html) |
| Integrate an IDE or agent client | [MCP Request Builder](interactive/mcp-integration.html) |
| Understand executor verification | [Writ Executor Walkthrough](interactive/writ-executor.html) |
| Work with multiple councils | [Multi-Council Router](interactive/multi-council.html) |

---

## Folder Map

| Folder | Purpose |
|--------|---------|
| [guides/](guides/README.md) | Step-by-step deployment guides: cluster council, executor placement, third-party agent integration. |
| [interactive/](interactive/README.md) | Browser-based explainers and planners. |
| [proposals/](proposals/README.md) | Design proposals, generated PDFs, and diagrams. |

## Deployment Guides

Interactive HTML walkthroughs with topology diagrams, switchable scenes, and copy-able code blocks.

| Guide | When to read |
|-------|--------------|
| [1. Council Cluster Deployment](guides/01-council-cluster-deployment.html) | You operate the cluster and need to host the council as a shared service. |
| [2. Executor Placement](guides/02-executor-placement.html) | The council is up; you need to decide where writs actually run. |
| [3. Agent Integration](guides/03-agent-integration.html) | You have an existing agent and want it to call the council. Covers shipped executors and writing your own. |

---

## Operational Reference

| Document | What It Covers |
|----------|----------------|
| [Deployment Setup](deployment-setup.md) | Deployment modes, environment variables, Helm values, Compose propagation, and troubleshooting. |
| [Council MCP Validation](council-mcp-validation.md) | Post-deploy health checks, MCP contract tests, live council checks, and Compose smoke tests. |
| [README](../README.md) | Condensed overview, quick start, and architecture summary. |

## Interactive

Use these browser-based pages when onboarding someone who needs to understand or configure the system quickly:

| Guide | Best For |
|-------|----------|
| [Workflow Walkthrough](interactive/workflow.html) | Seeing a request move through monitor, council, OPA, writ minting, executor verification, and audit. |
| [Getting Started Walkthrough](interactive/getting-started.html) | Kind or Compose setup, account creation, smoke checks, and troubleshooting. |
| [Deployment Planner](interactive/deployment.html) | Choosing OPA mode, executor profiles, demo UI, exposure, and health checks. |
| [Writ Executor Walkthrough](interactive/writ-executor.html) | Understanding why executor verification can still reject a council-approved writ. |
| [Certified Evidence Verification](interactive/certified-evidence-verification.html) | How signed executor evidence is verified before the council sees it as certified. |
| [Authentication Explorer](interactive/authentication.html) | Understanding token auth, account state, council membership, and permission scopes. |
| [MCP Request Builder](interactive/mcp-integration.html) | Building correct `tools/call` JSON-RPC payloads for council tools. |
| [Multi-Council Router](interactive/multi-council.html) | Understanding v1/v2 routing, default council fallback, explicit `council_id`, and membership. |

## Proposals

| Document | What It Covers |
|----------|----------------|
| [Container Architecture](proposals/container-architecture.md) | Service boundaries, networks, runtime flows, trust boundaries, and failure behavior. |

---

## Quick Links

- **README**: [../README.md](../README.md) — condensed overview with quick start
- **Deployment**: [deployment-setup.md](deployment-setup.md) — Docker Compose entry points and profile selection
- **Council MCP Validation**: [council-mcp-validation.md](council-mcp-validation.md) — post-deploy validation ladder
- **Workflow Walkthrough**: [interactive/workflow.html](interactive/workflow.html) — current browser-based flow from intent to execution

---

## System at a Glance

```
Agent → Monitor MCP/gRPC call → OPA filter → Council Service → Council Members vote → Writ minted
                                                                  │
                                                         signed writ blob
                                                                  │
                                              Trusted Executor → verify writ → local policy → execute
                                                                                      │
                                                                        { stdout, stderr, exit_code }
```

### Three Services

| Service | Container | Port | Role |
|---------|-----------|------|------|
| **Redis** | `redis:7-alpine` | 6379 | Nonce store, accounts, rate limits, writ metadata |
| **Monitor / MCP** | `deploy-monitor` | 9901 | External MCP proxy, dashboard, and request metrics |
| **Council** | `deploy-council` | 9901 internal | MCP server with LLM-backed security council |
| **Council gRPC** | `deploy-council` | 9904 | Thin gRPC proxy to MCP through the monitor |
| **OPA** | `openpolicyagent/opa` | 8181 | Optional front-door intent policy filter |
| **Executor** | `deploy-executor-openshell`, `deploy-openshell`, or third-party | 9907 default | Trusted executor contract implementation; OpenShell is the default bundled executor |

### Key Design Principles

1. **No command runs without a signed writ.** The executor rejects anything that isn't cryptographically signed by the council.
2. **Defense in depth.** OPA can deny before LLM review; council approval is necessary but not sufficient; the executor enforces its own local policy (tool whitelist, denied args, blocked patterns).
3. **Lakos-style levelization.** Each layer depends only on layers below it. Level 0 has zero external dependencies.
4. **Separation of concerns.** Executors conform to the `trusted_executor_contract` HTTP surface; bundled implementations live under `executors/` (`docker` for local subprocess, `openshell` for OpenShell).
5. **Profile-driven sandboxing.** Each executor instance loads a YAML config that defines exactly what tools, paths, and arguments are allowed.
