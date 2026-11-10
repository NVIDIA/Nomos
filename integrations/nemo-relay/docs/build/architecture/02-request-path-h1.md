<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# 2. H2: LLM execution interceptor and request correlation

[Architecture index](README.md) · [Migration guide and before/after examples](../native-correlation.md)

**H2, the LLM execution interceptor,** observes both the model request and its completed response.

## H2 entry: capture before provider dispatch

The authenticated native scope snapshot identifies the application Agent. The request codec decodes the offered
schemas and messages. `_prepare_request` captures immutable internal state, injects the four business intent fields,
and optionally appends the evidence catalog. It never sends an internal request identifier to the model.

{{code nemo_relay_nomos/plugin.py NomosPlugin._native_agent}}

{{code nemo_relay_nomos/plugin.py NomosPlugin._prepare_request}}

## H2 completion: bind returned tool-call IDs

Each callback retains its own snapshot while awaiting `next_.call`. A response finishing before another cannot
change the other's origin. The worker binds every returned provider ID under the native Agent UUID, including
wrapper calls the host later resolves into guarded leaf tools. H3 still authorizes the actual operation.

{{code nemo_relay_nomos/plugin.py NomosPlugin.native_unary}}

{{code nemo_relay_nomos/repository.py EvidenceStore.bind_native_calls}}

H2 comprises the `correlate-request` (unary) and `correlate-stream` (streaming) registrations.
H3 retrieves the association for authorization; it does not capture the request snapshot.

## Streaming

The worker receives raw chunks without a response codec. Only OpenAI chat chunks are supported: a built-in
`openai_chat` identity or an explicit `stream_format="openai_chat"` operator declaration is required. Callback
codecs need that declaration. Chunks pass through unchanged, while bounded IDs/name fragments are collected.
Bindings publish only after terminal tool-call completion, normal stream exhaustion and successful cleanup.
Cancellation, malformed or incomplete proposals publish nothing.

{{code nemo_relay_nomos/plugin.py NomosPlugin.native_stream}}

## What the model sees

Four required intent fields and optional evidence citations; no correlation metadata or retry
instructions containing a request token. Catalog text is present only when there is visible, citable evidence
owned by the same native Agent. Its entries never include tool output.

{{code nemo_relay_nomos/llm_request.py nomos_field}}

{{code nemo_relay_nomos/llm_request.py render_message}}

## Host contract and lifetime

The model and tool callbacks must preserve native Agent ownership and the provider call ID. Human-approval replay
must restore both. Missing, expired or ambiguous bindings refuse; restart loses all in-memory origins. A native
Agent UUID is neither an authenticated user account nor a persistent chat identifier. Another Agent does not inherit
evidence by copying a transcript. See the [integration requirements](../native-correlation.md#safety-and-integration-requirements).
