<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Boundaries before a live agent test

The completed experiment is a context-service/reader component capture and Jev
model-input study. It does not establish that a Hermes/Council deployment is
ready, that tools actually ran, or that an HTTP request is attributable to one
specific tool call. The synthetic records represent those events.

A live agent extension requires separate acceptance checks:

1. Qualify the actual Hermes, Relay host and worker SDK version combination.
   A recorded earlier inspection found Hermes requiring Relay >=0.10,<0.11;
   environment forwarding is a newer capability. Recheck current upstream
   compatibility rather than forcing a dependency upgrade. The worker also
   supports a literal `context_service.sandbox_name` setting.
2. Deploy authenticated context publication/lookup and TLS/JWT reader access
   with protected credentials. Keep the context API outside recursive review.
3. Verify real H1 snapshot and H3 lifecycle publication, a harmless Council
   approval, a Council refusal, and the resulting HTTP assessment separately.
   A Council refusal preventing HTTP is not a middleware decision.
4. If using an isolated Docker gateway, configure a distinct sandbox label as
   well as separate identity, state, ports and signing material. Inspect current
   OpenShell configuration and cleanup behavior before creating a gateway.
5. Obtain the agent and Council model/provider configuration through approved
   deployment channels. Public study artifacts contain no private setup assets.

No existing gateway or sandbox was reconfigured by the completed pilot.
Host inventory, local account details, internal runbooks and unrelated provider
probes are deliberately excluded from this public handoff.
