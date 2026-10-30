<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Optional Nomos conversation context service

An experimental, standalone HTTPS API for short-lived conversation snapshots and
associated tool-call records. It has its own **single-process in-memory store**;
it does not use the Council database, perform Council review, or call a model.

The intended clients are our Nomos Relay plugin (publisher) and Nomos OpenShell
middleware (reader). **Those opt-in client integrations are not implemented by
this directory.** Existing plugins and deployments are unchanged. They must
remain usable with context publishing/lookup disabled.

```text
Relay H1: existing retained context ── publish copy ─┐
Relay H3: tool lifecycle event ── publish record ────┤
                                                   ▼
                                    Context API + in-memory TTL store
                                                   ▲
OpenShell middleware ── authenticated name lookup ──┘
```

The sole per-sandbox lookup key is `sandbox_name`. Each name must be unique
across **every gateway and workspace sharing this service**, and must not be
reused for another sandbox or conversation, even after expiry. A name is not
an authenticated workload identity. There are no namespaces or binding IDs.

## Start locally

Run these commands from `integrations/conversation-context`, using Python
3.11–3.14 and [uv](https://docs.astral.sh/uv/). This is an independent Python
project: its dependencies are not added to the root Nomos environment.

Generate two distinct credentials without printing them or putting them in
command-line arguments. Keep the files outside the repository:

```sh
export NOMOS_CONTEXT_PUBLISHER_TOKEN_FILE="$HOME/.config/nomos-context/publisher-token"
export NOMOS_CONTEXT_READER_TOKEN_FILE="$HOME/.config/nomos-context/reader-token"
python3 - <<'PY'
import os
import secrets
from pathlib import Path

for variable in ("NOMOS_CONTEXT_PUBLISHER_TOKEN_FILE", "NOMOS_CONTEXT_READER_TOKEN_FILE"):
    path = Path(os.environ[variable])
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Exclusive creation avoids accidentally rotating an existing deployment.
    with path.open("x", encoding="utf-8", opener=lambda p, f: os.open(p, f, 0o600)) as file:
        file.write(secrets.token_urlsafe(32) + "\n")
PY

uv run --locked --no-dev nomos-context-service
```

The default endpoint is `http://127.0.0.1:18083`. Both token files are required,
including for a local run. The program reads file **paths** from the two
variables above; the corresponding `--publisher-token-file` and
`--reader-token-file` flags override those paths. There are no built-in tokens.
Only token hashes are retained for API authentication after app construction.
Rotate credentials by replacing both relevant client configuration and server
files, then restarting the server. Restarting clears context.

### HTTPS and Kubernetes

For an endpoint reachable by sandboxes and the middleware, configure a trusted
certificate and key and bind only to the required interface:

```sh
uv run --locked --no-dev nomos-context-service \
  --host 10.0.0.20 --port 18083 \
  --tls-cert-file /run/tls/context/tls.crt \
  --tls-key-file /run/tls/context/tls.key
```

A nonloopback bind without TLS is refused unless `--allow-insecure-lab` is
explicitly supplied. That flag permits plaintext transport; **authentication
still applies**. Use it only for an isolated lab or an appropriately secured
TLS-termination path. DNS names are not implicitly treated as loopback.

In Kubernetes, deploy one replica running one worker, mount the two token
files using Kubernetes Secrets, and expose an internal Service. Supply the TLS
certificate/key or terminate TLS through your approved ingress/proxy setup.
Restrict network access to the intended callers. A `ClusterIP` is not an
access-control mechanism. Do not scale replicas/workers independently: their
in-memory stores would disagree. Horizontal scaling requires a shared-store
implementation and a separate design review.

The service does not modify OpenShell policy. Publication endpoints must have
explicitly permitted sandbox egress and must be excluded from any context lookup
that would otherwise require a snapshot before the first publication. Existing
OpenShell network/credential checks continue to apply.

## HTTP API

All application routes require `Authorization: Bearer <token>` and uncompressed
UTF-8 `application/json`. The server authenticates before reading/parsing the
body, bounds actual streamed bytes, and rejects duplicate JSON keys, nonfinite
numbers, unknown fields and invalid record shapes. Error responses contain a
stable `detail` reason, not the submitted conversation or validation input.
Responses set `Cache-Control: no-store`. Proxies must not cache these routes.

| Route | Credential | Behavior |
| --- | --- | --- |
| `GET /health` | None | Fixed `{"status":"ok"}`; no names, content or store statistics |
| `POST /v1/context-snapshots` | Publisher | Publish selected model-request context |
| `POST /v1/tool-records` | Publisher | Append one immutable event linked to a live snapshot |
| `POST /v1/context-lookups` | Reader | Read metadata by name; content requires two explicit opt-ins |

The publisher cannot read and the reader cannot publish. Credentials must be
distinct, at least 32 ASCII characters, with no whitespace; generate random
values rather than human passwords. The current pilot has **one publisher
credential authorized for all names**, so it provides no isolation between
mutually untrusted publishers. Anyone holding that token can write another
name. Do not use this deployment for a multi-tenant trust boundary.

### Publish a snapshot

For the planned Relay client, reuse H1's existing retained turn context: the
latest user request (up to 800 characters under existing policy), its truncation
flag and relevant retained bookkeeping. Do not add a new full-history snapshot
implementation in H1. The server's generic `context` object can hold that existing
record; its transport envelope does not change H1's retention controls. H3 adds
the separate tool-call events. These clients are still future integration work.

Illustrative body; generate fresh IDs and a future deadline at the actual call:

```json
{
  "sandbox_name": "hermes-unique-a7f39c",
  "snapshot_id": "snapshot-001",
  "model_attempt_id": "attempt-001",
  "model_name": "configured-agent-model",
  "not_after": "2026-10-30T12:04:00Z",
  "context": {
    "latest_user_request": "Prepare the fictional workshop announcement.",
    "user_request_truncated": false,
    "tool_definitions_sha256": null,
    "tool_definitions_kept": false
  }
}
```

`not_after` is a timezone-aware UTC deadline. The server rejects a deadline
already reached (`410`) or farther away than `--ttl-seconds` (`422`). For a
300-second server ceiling, a client can choose `now + 240 seconds` to leave
clock-skew margin. Synchronize client/server clocks. The client may choose
shorter retention, but cannot increase the server ceiling.

The server returns `201` with the ID, commit revision, server `received_at`,
accepted `expires_at`, and `duplicate: false`. The accepted expiry is the fixed
`not_after` value, not a new lifetime added on each retry. Reuse the **identical
request**, deadline and IDs for a bounded transport retry. An identical live
retry returns `200` and the original receipt; conflicting live ID reuse returns
`409`. A retry cannot make an older snapshot become latest.

The fixed deadline is important: after an expired payload is purged or the
server restarts, the exact old request is still expired and returns `410`.
There is no permanent tombstone or deduplication database. Clients must use
fresh unique IDs for genuinely new publications; changing a deadline makes a
new request, not a legitimate retry. A malicious publisher can submit old
content with fresh IDs/deadlines. This service does not attest content freshness
or truthfulness.

Snapshots expire on either the fixed UTC deadline or the corresponding local
monotonic deadline. Expired records are inaccessible immediately. An `asyncio`
task on the serving event loop (not a separate thread) removes idle expired
payloads every 60 seconds.
Publications and lookups also remove expired entries. During idle periods, expired
payloads may remain in memory until the next scan; a busy event loop can delay
cleanup further without making them readable. The most recently committed
snapshot is the current view. **If it expires, lookup returns unavailable rather
than falling back to an older still-live snapshot.** A fresh publication can
establish a new current view.

### Append a tool event

```json
{
  "sandbox_name": "hermes-unique-a7f39c",
  "record_id": "tool-event-001",
  "snapshot_id": "snapshot-001",
  "tool_call_id": "provider-call-001",
  "tool_name": "terminal",
  "state": "execution_pending",
  "arguments": {"command": "printf fictional"},
  "council_review": {
    "council_id": "council-example",
    "decision_id": "intent-example",
    "status": "approved"
  }
}
```

The parent snapshot must still exist under that same sandbox name. The event
inherits the parent's expiry; it cannot renew it. Allowed states are
`requested`, `denied`, `execution_pending`, `completed`, `failed`, and
`execution_unknown`. These are publisher reports, not server attestations.

Use a different `record_id` for each lifecycle transition. Do not overwrite
`execution_pending` with `completed`. Identical live retries return `200`;
conflicting live reuse returns `409`. A missing/expired parent returns `404`.
`tool_call_id` and selected/redacted `arguments` are optional. Provider tool-call
IDs and observed request/sandbox IDs are opaque strings of 1–256 characters;
they are not restricted to the generated record-ID alphabet. The server does
not redact supplied content: clients must select, bound and redact it before
publication. No tool result body is part of this version's event schema.

### Planned publisher failure behavior

The Relay publisher is follow-up work. Its context-service calls must always
fail open on runtime publication errors: connection failures, timeouts, error
statuses and invalid responses are logged without interrupting the normal
model/tool path. Apply this to snapshot, pre-tool, Council denial and completion
publications. Keep attempts bounded; never rerun model/tool execution to repair
a context write, replace the tool result, suppress cancellation, or bypass
Council approval and writ validation. Local configuration errors are still
validated at activation.

A failed write may leave an absent event or older unexpired context. A timeout
may also mean that a write committed but its acknowledgement was lost. The
reader must not treat stored context as proof of complete current coverage.
OpenShell reader behavior is configured independently: best-effort reading
falls back to HTTP-only review, while required reading can block HTTP when
context is unavailable.

### Council review and tool execution are separate

Each immutable tool-event record is stored by the context service through
`POST /v1/tool-records`. Its `council_review` object records selected fields from Council's
[`AuthorizeOutput`](../../nomos/nat/functions.py): `status`, `decision_id`,
`council_id` and `guidance`. The decision ID is Council's Intent/decision ID,
not a new identifier generated by this service. Existing records without this
object are still accepted, including events before review or calls not reviewed
by Council. This API allowance is not permission for the planned H3 publisher
to omit a known review: after a Council response or connection/timeout error,
include `council_review` and retain it on later execution events. Connection
errors use `status: error`, without an invented decision ID.

| Field | What it means |
| --- | --- |
| `state` | What happened to the tool call: requested, prevented from executing (`denied`), about to execute, completed, failed, or unknown |
| `council_review.status` | Council's reported response: `approved`, `denied`, or `error` |
| `council_review.decision_id` | Optional opaque reference to that Council review, 1–256 characters when supplied |
| `council_review.council_id` | Optional opaque Council identifier, 1–256 characters when supplied |
| `council_review.guidance` | Returned advice, default `[]`; at most five nonempty strings of up to 1,024 Unicode characters each |
| `council_review.guidance_truncated` | Publisher reports that guidance was shortened or omitted, default `false` |

For example, a Council refusal produces a record like this (common identity
fields omitted from this illustration):

```json
{
  "tool_call_id": "provider-call-001",
  "state": "denied",
  "council_review": {
    "decision_id": "intent-example",
    "status": "denied",
    "guidance": ["Remove the credentials before uploading the file."],
    "guidance_truncated": false
  }
}
```

An approved tool may subsequently fail to execute. Its separate
`execution_pending` and `failed` records retain the same approved review
reference. Even `state: denied` can follow Council approval if later writ
validation prevents execution; do not infer Council's verdict from `state`.
A Council error can have no decision ID and is not a policy denial. Omit empty
source IDs instead of inventing values. No state transition is created or
verified by this service: the publisher must append each event explicitly.

The planned Relay publisher must record actual returned Council status and
available IDs, include selected/redacted denial guidance, and publish a denial
without invoking the tool. Do not manufacture guidance or translate `error`
into a Council denial. Clip guidance deliberately before publication and set
`guidance_truncated` when shortened or omitted; server validation rejects excess
rather than silently truncating it. These character bounds follow Relay's
existing denial renderer; raw-request and aggregate byte limits still apply.
Carry the review reference into later completion/failure events and preserve
the parent's fixed expiry.

Review fields, including guidance and the truncation flag, are untrusted
publisher reports, not verified Council evidence. They are included only in
explicit content lookups, never metadata-only responses. Do not publish writs,
authentication tokens, complete Council responses, votes or raw error bodies.
Existing Council and OpenShell authorization checks remain independent.
The standalone server supports these fields; Relay publication remains future
integration work.

### Lookup from the middleware

```json
{
  "sandbox_name": "hermes-unique-a7f39c",
  "observed_sandbox_id": "openshell-sandbox-123",
  "http_request_id": "http-request-884"
}
```

Only `sandbox_name` selects data. The optional observed IDs do not change the
key, establish an identity binding, or prove which tool caused the HTTP request.
The caller must authenticate the OpenShell supervisor and validate those IDs in
the middleware; this server does not validate OpenShell JWTs for it.

The default response contains the latest snapshot reference, revision, expiry,
and up to 20 recent live tool-event references under the name, newest first. It
sets `tool_records_truncated` if more events exist, `source_content_omitted` to
true, and `exact_tool_call_id_for_http_request` to null. Missing/expired current
context returns `404 context_unavailable`.

An authenticated reader receives full selected context when it explicitly
requests `"include_content": true`; no additional server flag is needed. Omit
that field or set it to false for metadata only. A publisher token, invalid
token or missing token cannot read either view (`401`).
The content response also includes the latest snapshot and the selected tool
events. Older events may refer to earlier still-live snapshots; the service
does not assert that any event caused this HTTP request. Initial audit-only
clients should use the default metadata view.

## Limits, retention and failure behavior

| Setting | Default | Meaning |
| --- | --- | --- |
| `--ttl-seconds` | 300 | Maximum accepted future snapshot deadline; tools inherit the parent's expiry |
| `--max-body-bytes` | 262144 (256 KiB) | Actual raw bytes per API request, before JSON parsing |
| `--read-timeout-seconds` | 5 | Maximum time spent receiving a body |
| `--max-records` | 1000 | Combined live snapshot and tool-event count |
| `--max-store-bytes` | 16777216 (16 MiB) | Sum of retained canonical record JSON bytes, not process RSS |

Capacity exhaustion returns `507 store_capacity_reached`. It never evicts live
records to accept a new write. Expiry frees capacity. Parsing, indexes, response
construction, HTTP buffers and Python objects consume memory beyond the stored
byte budget; use deployment memory/concurrency limits as well. The executable
sets Uvicorn's concurrency limit to 32, disables access logs and forwarded-proxy
header trust, and runs exactly one worker. Normal Uvicorn startup/error logs do
not intentionally emit request bodies or credentials; detailed per-publication
structured events are not implemented.

Nothing is written to the Council ledger, Redis, PostgreSQL or disk by this
component. Restart loses all snapshots. Application TTL does not guarantee
cryptographic erasure from RAM, crash dumps or host swap; deploy under your
normal sensitive-data controls and do not add body-logging or backups.

The service returns errors; the clients decide how to enforce them. The planned
Relay feature is disabled by default. When explicitly enabled, required
pre-execution publications must be acknowledged before continuation; errors
must not silently disable the feature or trigger tool re-execution. Middleware
lookup is separately disabled by default, with explicit audit or required
behavior. A service error alone is not a content denial.

## Verify and inspect the implementation

```sh
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest --cov=nomos_context_service --cov-report=term-missing --cov-fail-under=90
```

- [models.py](src/nomos_context_service/models.py): strict request schemas.
- [store.py](src/nomos_context_service/store.py): expiry, immutable writes and atomic lookup.
- [server.py](src/nomos_context_service/server.py): authentication, HTTP boundaries and CLI.
- [test_service.py](tests/test_service.py): API and concurrency/retention checks.
- [test_cli.py](tests/test_cli.py): deployment and credential-file checks.
- [Dependency inventory](THIRD_PARTY_NOTICES.md): separate lockfile and upstream packages.

Tests are synthetic and require no model, Council, Docker or OpenShell instance.
They do not certify the future Relay client, middleware client or Kubernetes
rollout. This service is optional; installing it does not enable those clients.
