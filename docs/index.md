<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Nomos Documentation

Nomos stops agents from taking unwanted or overeager actions. It holds each
proposed action for policy and council review against the agent's own context:
its permissions, its task, its justification and the signed receipts of what it
has already observed. Approval
produces a signed, single-use **Writ of Execution**; the agent hands it to an
executor, which verifies it, runs exactly the approved command and returns a
signed receipt. Start with the [README](../README.md) for the one-page overview.

## Choose your next step

| Your goal | Start here |
|---|---|
| Understand what Nomos does and how a request flows | [Architecture](architecture.md) |
| Run Nomos locally and make a first request | [Getting started](getting-started.md) |
| Put your existing agent behind Nomos | [Integrate your own agent](integrate-your-agent.md) |
| Change members, models, policy or executors | [Configuration](reference/configuration.md) |
| Deploy or operate a council | [Deployment setup](deployment-setup.md) |

## Learn, in order

1. [Architecture](architecture.md): the request path step by step, the Writ, the
   components and what each one is trusted with.
2. [Getting started](getting-started.md): Linux prerequisites, Kind or Compose,
   verifying the stack, accounts, a first end-to-end request.
3. [Integrate your own agent](integrate-your-agent.md): the five-step adapter,
   a complete Python client, IDE agents, application tools, custom executors.
4. [Denial recovery](agent-denial-recovery.md): what an agent should do with a
   denial, an error, or an overload response.
5. [Configuration](reference/configuration.md): council members and models,
   council and executor config files, executor profiles.

## Deploy and operate

| Document | What it covers |
|---|---|
| [Deployment setup](deployment-setup.md) | Deployment modes, environment variables, Helm values, Compose propagation, troubleshooting |
| [Self-hosted models](self-hosted-models.md) | A local NIM on Compose or Kind, pure or mixed routing, existing model servers |
| [Council MCP validation](council-mcp-validation.md) | Post-deploy health checks, MCP contract tests, live council checks, Compose smoke tests |
| [`deploy/README.md`](../deploy/README.md) | Service-mode Compose bootstrap (PostgreSQL, Vault, gateway) |
| [`deploy/kubernetes/README.md`](../deploy/kubernetes/README.md) | Service-mode Kind with ingress |

## Reference

| Document | What it covers |
|---|---|
| [MCP tools and executor API](reference/mcp-api.md) | The council's MCP tools, structured `tool_call` requests, the trusted executor HTTP contract |
| [Configuration](reference/configuration.md) | `configs/council.yaml`, executor YAML, executor profiles, risk-based Writ lifetimes |
| [Council protocol skill](../skills/council-protocol/SKILL.md) | Loadable instructions for agents that write council requests |
| [Development](development.md) | Repository layout, tests, the in-process kernel example |

## Interactive explainers

These HTML pages run in a browser: open them from a local checkout. On GitHub
they show as source. Each topic is also covered by the Markdown pages above.

| Page | Best for |
|---|---|
| [Workflow walkthrough](interactive/workflow.html) | Watching a request move through the monitor, OPA, the council, Writ signing, executor verification and the ledger |
| [Getting started walkthrough](interactive/getting-started.html) | Stepping through Kind or Compose setup |
| [Deployment planner](interactive/deployment.html) | Choosing OPA mode, executor profiles, demo UI and exposure on Compose |
| [Writ executor walkthrough](interactive/writ-executor.html) | Why an executor can still reject a council-approved Writ |
| [Certified evidence verification](interactive/certified-evidence-verification.html) | How a signed receipt is verified before reviewers see it as certified |
| [Authentication explorer](interactive/authentication.html) | Tokens, account state, council membership and permission scopes |
| [MCP request builder](interactive/mcp-integration.html) | Building `tools/call` payloads for council tools |
| [Multi-council router](interactive/multi-council.html) | v1/v2 routing, default council fallback, explicit `council_id` |
| [Guide 1: Council cluster deployment](guides/01-council-cluster-deployment.html) | Hosting the council as a shared cluster service |
| [Guide 2: Executor placement](guides/02-executor-placement.html) | Deciding where Writs run: host, fleet or management host |
| [Guide 3: Agent integration](guides/03-agent-integration.html) | Shipped executors and writing your own |

## Design records

[Proposals](proposals/README.md) are dated design documents. They describe the
system as it was when written; the pages above describe it as it is.

| Document | Status |
|---|---|
| [Container architecture](proposals/container-architecture.md) | Historical: predates service mode (PostgreSQL, Vault, gateway) |
| [HA nonce store](proposals/06_ha_nonce_store.md) | Design proposal |
| [Council stats via Redis](proposals/13_council_stats_via_redis.md) | Design proposal |
