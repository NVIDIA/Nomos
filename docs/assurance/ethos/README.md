<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Operator-managed ETHOS documents

Agent owners author and review purpose contracts before enabling ETHOS analysis.
Eval Author can assist offline; it is not part of periodic monitoring. Start from
[NOMOS_BASELINE.md](NOMOS_BASELINE.md) and [ROLE_TEMPLATE.md](ROLE_TEMPLATE.md).
The baseline is shared authoring guidance, not a universal default assignment.

[mapping.example.json](mapping.example.json) maps one example identity to the
standalone [DEPLOYMENT_EXAMPLE.md](DEPLOYMENT_EXAMPLE.md). Replace its mission,
identity and effective dates with owner-approved configuration before real use.
Paths are relative to the mapping file, or absolute mount paths. Mount documents
and the mapping read-only in Compose or Helm. Only existing authenticated agent
IDs are mapped; this creates no new Nomos authorization role.

Each mapping declares a document ID, version, effective interval and requirements.
Every requirement's ID and full text must occur verbatim in the Markdown. Declare
its minimum evidence classes: `request`, `execution`, `conversation`. Make each
requirement self-contained, including its scope and exceptions, because only
eligible requirements are sent to Compass. A rule about requested targets may
need just `request`; an action rule needs `execution`; a consent or reporting rule
needs `conversation`. A requirement combining classes needs all of them.

Unknown identities and gaps in effective history report `not_configured`.
Missing evidence reports `insufficient_evidence` for the affected requirement.
Tool checks continue. Native executor events need explicit token/intent links to
establish the agent; cited receipt summaries alone do not establish that the
reviewing agent performed an execution. Conversation evidence must use the signed
`assurance_context` v1 extension with `conversation_complete: true` and captured
`messages` of `{role: user|assistant, content: string}`. Arbitrary intent metadata
and council opinions cannot establish a human conversation.

Documents are immutable during a service configuration snapshot. Edit through
version control and review, create a new version with a new effective interval,
retain old files and add the historical versions to that agent's mapping. End the
old interval exactly when the new one begins; overlapping assignments are
rejected. Roll out configuration explicitly. Runs record content hashes and the
mapping digest; later file edits do not mutate a running registry. Findings and
exports retain the original contract. The service never generates or rewrites
its ETHOS. One document may serve many agents with the same responsibilities.

Configured ETHOS text and selected evidence are sent to the approved inference
endpoint. Treat that endpoint as authorized for this data. Source projection
limits review fields; it is not a general DLP scanner. Do not put secrets in
contracts or ledger content. Operational manifests omit inference credentials.
