<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Test destination server

This small FastAPI server records the HTTP requests that reach it from an
OpenShell sandbox. It provides forwarding evidence independently of the
middleware's decision logs. It performs no model assessment or authorization.

Use only fictional data in an isolated lab. There is no authentication:
anyone who can reach `/requests` can inspect recorded bodies. Base64 is a
reversible encoding, not encryption. The default listener is loopback.

## Start and inspect

From `integrations/openshell`, with [uv](https://docs.astral.sh/uv/getting-started/installation/)
and Python 3.11–3.14 available:

```sh
uv run --locked --no-dev --project tools/test-destination \
  test-destination --host 0.0.0.0 --port 18081
```

The explicit wildcard bind lets a Docker bridge supervisor reach the host
service. Restrict access to the lab network. Omit `--host` for loopback-only
development. Keep this process running in its own terminal.

From the host:

```sh
curl --fail http://127.0.0.1:18081/health
curl --fail http://127.0.0.1:18081/requests
```

Follow [the lab setup](../../docs/lab-setup.md) to build a curl workload, start
the separate middleware process, configure a bridge gateway and send requests
through OpenShell. A direct host-side POST bypasses OpenShell and only tests
the destination itself.

## Routes and records

`/publish` accepts any HTTP method. Additional capture routes are `POST /submit`,
`POST /support`, `POST /messages`, `PUT /documents/{document_id}` and
`POST /repos/{owner}/{repo}/issues`. The issue-shaped route is local capture
only; it makes no external requests or repository changes. The example
OpenShell policy permits only `/publish`; additional server routes do not
grant sandbox egress permissions.

The capture handlers preserve raw bytes without parsing or reformatting JSON,
including non-JSON bodies. This is intentional: content validation belongs to
the middleware, and the destination should record anything that reaches it.

`GET /requests` returns:

| Field | Meaning |
| --- | --- |
| `received_count` | Calls that entered a capture handler, including calls later rejected by this server |
| `recorded_count` | Complete bodies retained |
| `requests[].request_id` | Server-assigned arrival number; unrelated to OpenShell's request ID |
| `requests[].method`, `requests[].path` | HTTP method and path observed by this server |
| `requests[].body_bytes` | Original body length in bytes |
| `requests[].body_base64` | Original bytes encoded reversibly as base64 |
| `requests[].sha256` | SHA-256 of the original bytes |

For example, `GET /requests?after=3` returns only records with an arrival ID
greater than 3; the counts remain global. Inspection and readiness calls do
not increment arrival counts. Compare counts before and after each sequential
sandbox request, and compare hashes or decoded bytes for allowed requests.
A missing arrival alone does not prove content denial: network failures and
early server rejections also prevent capture. Correlate OpenShell and
[middleware logs](../../docs/logging.md).

Bodies have a 256 KiB limit and a five-second read deadline. Oversized bodies
return 413; read timeouts return 408. The server retains at most 100 bodies by
default, returning 507 when full rather than evicting evidence. These failures
still increment `received_count` once the capture handler has started.
Use `--max-records NUMBER` to change capacity; exporting does not free slots.
HTTP-server overload can reject a request before the handler runs.

Records are memory-only. Run one worker, as the launcher does. Stop with Ctrl+C
and restart to clear the store. There is no reset endpoint or durable storage.

## Check changes

From `integrations/openshell`:

```sh
uv run --locked --project tools/test-destination ruff check tools/test-destination
uv run --locked --project tools/test-destination ruff format --check tools/test-destination
uv run --locked --project tools/test-destination pytest tools/test-destination/tests
```

These tests cover exact-byte preservation, capture counts, size/capacity
rejections, method/path evidence, cursor inspection and restart behavior.
They do not establish model quality or end-to-end OpenShell compatibility.
The independent Python dependencies are recorded in `uv.lock` and
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md); no benchmark tooling is required.
