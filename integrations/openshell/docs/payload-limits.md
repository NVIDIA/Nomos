<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Payload limits and model context

The service uses two configurable byte gates. They bound different messages;
neither guarantees that an input fits a model's token window.

| Gate | Default | What is counted |
| --- | --- | --- |
| Original inspected HTTP body | 262,144 bytes (256 KiB) | Actual body bytes, before JSON parsing |
| gRPC transport message | 524,288 bytes (512 KiB) | Each complete incoming or outgoing protobuf message |

`Describe` advertises the body limit in the HTTP-request binding's
`max_payload_bytes`. Configure the OpenShell registration at or below that
value. OpenShell owns HTTP framing and buffering; the service independently
checks the actual body length before parsing. The transport decoder applies its
limit before invoking the handler. A transport cap is not a strict bound on
decoded heap use, JSON parsing memory or CPU consumption.

## Why these defaults?

These are coarse engineering defaults, not a tokenizer-derived safety proof.
For illustration, suppose a model accepts a 32,000-token context and English
text averages four characters per token, each occupying one UTF-8 byte:

```text
32,000 tokens × approximately 4 characters/token × 1 byte/character
    ≈ 128,000 bytes = 128 decimal kB = 125 KiB
```

For Chinese text, an illustrative assumption of 1.5 tokens per character and
three UTF-8 bytes per character produces:

```text
32,000 tokens ÷ approximately 1.5 tokens/character × 3 bytes/character
    ≈ 64,000 bytes = 64 decimal kB = 62.5 KiB
```

Actual ratios depend on the model's tokenizer and content. These examples
describe context, not just the body: metadata and questions also occupy tokens.
They explain why a byte count cannot guarantee token acceptance across
languages. They are not claims about any configured model's context window.

The **256 KiB raw-body gate** is approximately twice the illustrative English
baseline, rounded to a power of two. It leaves room for formatting whitespace
that disappears when JSON is compactly serialized and for encoding variation.
Whitespace inside strings remains content. A formatted body might shrink
substantially, but the service imposes no separate post-minification body or
complete model-request byte limit. The model may still reject an admitted input.

The **512 KiB gRPC gate** leaves another 256 KiB beyond the body allowance for
context, target, headers, question configuration and protobuf encoding. This is
conservative headroom, not a proven worst-case envelope size. A body fitting
its own gate can still produce an oversized complete gRPC message.

Choose values for your deployment's memory, concurrency, workload and model
behavior. This integration neither tokenizes locally nor truncates content to
force acceptance. Treat upstream context rejection as an assessment failure.

## Configure both layers

```sh
cargo run --locked --bin nomos-request-review -- \
  --max-body-bytes 262144 \
  --max-grpc-message-bytes 524288
```

The [required model settings](configuration.md#process-settings) must already
be provided through the environment. Both flags accept integer byte counts from
1 to 4,294,967,295, with body limit strictly smaller than transport limit. The
upper bound follows gRPC's 32-bit message-length field, not a recommendation to
allocate messages that large. Other infrastructure or model limits may be smaller.

Keep the registration's `max_payload_bytes` coordinated with the advertised
body cap; see [setup](setup.md#3-register-and-select-traffic). Restart the
service and update the affected OpenShell deployment when changing settings.
Editing example files alone does not update running sandboxes.

## Errors and independent defensive bounds

| Condition | Result |
| --- | --- |
| Incoming message exceeds the transport cap | Tonic rejects before the handler; the locked decoder reports `OutOfRange` |
| Actual body exceeds its cap | `ResourceExhausted`, `body_over_limit`; no model call |
| Model returns HTTP 413 | `ResourceExhausted`, `model_input_over_limit`, without reading its response body |
| Model returns HTTP 400 with explicit `max_tokens_exceeded` code | `ResourceExhausted`, `model_input_over_limit` |
| Other model HTTP error, including schema-validation 422 | Sanitized `Unavailable`, `assessment_unavailable` |
| Invalid JSON, duplicate keys, non-object body or unsupported encoding | `InvalidArgument`; no model call |

The explicit token code is recognized at `code`, `error.code`, `detail.code`,
or an exact string value of `error` or `detail`. Unknown errors are not guessed
from human-readable message substrings. Provider error bodies are never copied
into gRPC statuses or logs. Under `on_error: fail_closed`, failures block
forwarding; OpenShell chooses the eventual HTTP response to the workload.

Independent bounds remain: 16 KiB encoded policy configuration, 1,024 UTF-8
bytes per question, 64 KiB per model HTTP(S) response body, and four active
evaluations with no queue. Metadata and parser nesting are validated too.
The **64 KiB response cap applies to the model HTTP(S) connection**, separately
from the OpenShell-to-service gRPC cap; it includes HTTP error bodies except
413, which is classified immediately. Oversized model responses fail closed.

See [local boundary tests](testing.md) for verification. Passing those tests
does not establish a live model's token window or acceptable production latency.


## Supplementary conversation context

Optional [context review](context-review.md) has a separate **32 KiB aggregate
serialized context cap**, including metadata. Whole tool records are selected
under that cap and conservatively estimated remaining model headroom; neither
HTTP content nor questions are truncated to make room. The Relay producer caps
each complete tool-event publication at **16 KiB**. Lookup reception is bounded
separately at 512 KiB before parsing. These are byte limits, not verified token
counts, and do not change this service's original body/gRPC limits.
