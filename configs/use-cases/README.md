<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Opt-in council use cases

The [chat-agent profile](chat-agent/council.yaml) is a complete configuration for
a bespoke chat application receiving direct user requests. Its council id is
`council-chat-agent`. The application supplies the latest request and relevant
prior conversation in `context`, separately from tool/document observations in
`evidence` or genuine `certified_evidence`. See the
[task-support and trust guide](../../docs/guides/causal-attribution-review.md)
for examples, routing, prerequisites, and verification limits.

Select the configuration explicitly from the repository root. The existing
standalone runner provides an interactive `propose_intent` session:

```sh
uv run python -m nomos.nat.run --config configs/use-cases/chat-agent/council.yaml
```

For the existing NAT MCP service, select the single-file catalog through the
workflow's environment substitution:

```sh
COUNCIL_CATALOG_PATH=configs/use-cases/chat-agent/council.yaml uv run --extra nat nat mcp serve --config_file configs/council_nat_workflow.yaml
```

The chat application selects `council_id: "council-chat-agent"`. For explicit
MCP routing, use `council_v2_propose_intent` or `council_v2_authorize` with
`api_version: "v2"` and that id. An id alone does not load a profile. The
explicitly loaded single profile is that process's default; unknown or disabled
ids fail through existing routing.

Provision provider credentials for all environment variables referenced by the
loader, an authenticated agent with membership in this council, Redis
credentials and account-integrity key, OPA availability, and writable configured
key/ledger locations. The MCP command also requires the optional NAT
dependencies. This profile inherits the base deployment paths and service
addresses, so run it in a suitably provisioned environment. Keep the existing
permission, authentication, and evidence gates enabled.

There is no directory discovery or automatic enrollment. Normal council
configuration, the default catalog, deployment templates, existing custom
profiles, and running or seeded deployments receive no automatic rollout.
Offline checks use fixed authored responses and prove transport and parsing;
they do not measure model behavior or establish causality.
