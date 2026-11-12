<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Trusted tools for a council

Define bespoke application tools in a council's YAML so reviewers know what each
tool does, which arguments it accepts, and when it may be used. Nomos takes these
definitions only from operator configuration. Agent descriptions, context,
evidence, metadata, and assertions about a tool cannot add or replace them.

A catalog entry is review context, not authorization. Structured calls still
require `tool:write`, council membership and authentication, applicable OPA
approval, and the existing council approval threshold. Even a tool described as
read-only requires `tool:write`. The catalog does not install tools, discover
executor capabilities, or grant user consent.

## Configure a profile

This complete single-council example can be saved as `warehouse-review.yaml`.
The backend address and model are illustrative: replace them with your deployed
review backend before serving requests. When adding tools to an existing
profile, preserve its authentication, backend, policy, and review settings.

```yaml
council_id: warehouse-review
require_auth: true
require_council_membership: true
require_justification: true
default_token_ttl_seconds: 300
llm_backends:
  local-review:
    provider: openai
    model: operator-review-model
    base_url: http://127.0.0.1:8000/v1
members:
  - id: safety-reviewer
    backend: local-review
    priorities:
      - Verify explicit user authorization, scope, and tool usage constraints.
opa:
  enabled: true
  fail_closed: true
tools:
  - name: warehouse.delete_rows
    description: >-
      Permanently delete up to limit rows from the named warehouse table.
      Deletion cannot be undone by this tool.
    parameters:
      $schema: https://json-schema.org/draft/2020-12/schema
      type: object
      properties:
        table:
          type: string
          enum: [scratch]
        limit:
          type: integer
          minimum: 1
          maximum: 5
      required: [table, limit]
      additionalProperties: false
    usage_constraints:
      - Only delete scratch data within the user's explicit authorized goal.
      - Confirm the selected rows before requesting deletion.
```

Each entry requires all four fields: `name`, `description`, `parameters`, and
`usage_constraints`. `parameters` is the full argument JSON Schema; it is not
just a properties mapping. Put machine-checkable restrictions in the schema.
Descriptions and usage constraints guide review; prose does not deterministically
enforce a precondition such as confirming selected rows.

The same `tools` field works in inline `councils` entries, referenced
`config_file` profiles, and `shared` configuration. Inheritance replaces the
whole list; entries are never merged by name. An omitted override inherits the
operator's normal shared/profile configuration. Use `tools: []` to clear an
inherited catalog.

After inheritance, these cases differ:

| Configuration | Structured authorization | Shell authorization and advisory propose |
| --- | --- | --- |
| `tools` omitted | Existing behavior, with no new catalog name/schema checks | Existing behavior |
| `tools: []` | Reject every structured tool name | Existing authorization with an empty trusted catalog |
| Nonempty `tools` | Require an exact, case-sensitive name and schema-valid arguments | Existing authorization with trusted tool availability |

`tools: null` is invalid. Unknown names and case variants fail closed; there is
no fallback to agent-supplied definitions. Each council uses only its own resolved
catalog. Two councils may define the same name with different meanings and
schemas; selecting one never searches the other's definitions.

## Authorize an exact invocation

Send the existing MCP `tools/call` request, substituting your agent credentials
and a subject derived by the calling host from its authenticated tenant/user.
The agent must have membership in `warehouse-review` and `tool:write` permission.
Transport authentication remains as described in the
[deployment guide](../deployment-setup.md).

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "council_authorize",
    "arguments": {
      "api_version": "v2",
      "agent_id": "warehouse-agent",
      "auth_token": "replace-with-agent-auth-token",
      "council_id": "warehouse-review",
      "description": "Delete three confirmed scratch rows",
      "justification": "The user explicitly requested cleanup of these scratch rows.",
      "tool_call": {
        "schema_version": 1,
        "tool_name": "warehouse.delete_rows",
        "arguments": {"table": "scratch", "limit": 3},
        "subject": "tenant-example:user-example"
      }
    }
  }
}
```

Structured calls use v2 and cannot include shell commands, working directories,
environment variables, or packaged files. The `council_v2_authorize` alias uses
the same structured path. With this catalog, `limit: 6`, a string limit, a
different table, or an extra argument fails validation before OPA, reviewers,
or writ minting. The response has status `error`, no writ, and an
`Invalid tool call:` error. Satisfying the schema only permits the request to
continue through the existing authorization gates.

`council_propose_intent` remains advisory shell/general review. It receives
trusted available-tool definitions but does not accept a structured invocation
for authorization; an extra `tool_call` there is ignored by the existing input
model. Use authorize above for a writ bound to a bespoke call. Nomos never infers
a structured invocation from prose.

All applicable reviewers, including debate, chairman/preauthorization, and
packaged-file review, receive the operator definitions as trusted system context.
Live arguments and requester claims remain untrusted review input. Approved
writs retain the existing exact tool-name, subject, and canonical argument-digest
binding. Changing a tool name, subject, or argument requires fresh authorization.

## Schema rules and limits

Use Draft 2020-12. Omit `$schema` to use that draft, or supply exactly the URI in
the example. The root must explicitly declare `type: object`. Nested schemas can
use standard composition, conditionals, arrays, boolean schemas, and `$defs`.

References are offline and document-local: `$ref` and `$dynamicRef` may use `#`,
JSON Pointer fragments such as `#/$defs/row`, or local anchors such as `#row`.
Targets must exist. External URLs, relative resource paths, file references,
URN references, and `$id` resource rebasing are rejected, including in unused
schema definitions. `$schema` selects the built-in dialect without fetching it.
Other drafts and custom vocabularies are unsupported.

`format`, content keywords, `default`, and unknown annotation keywords do not
enforce validity. For example, `format: email` alone does not reject a malformed
address; use structural assertions such as `pattern` for enforced restrictions.
Nomos does not decode content, inject defaults, coerce types, or transform
arguments. A string `"3"` cannot satisfy `type: integer`. JSON Schema considers
numeric `1.0` an integer; the existing canonical signed argument bytes still
distinguish `1.0` from `1`.

| Item | Limit |
| --- | --- |
| Tools per council | 32; unique names |
| Name | 1–128 ASCII characters, matching `[A-Za-z0-9][A-Za-z0-9_.:/-]*` |
| Description | 1–8000 characters, not whitespace-only |
| Usage constraints | 1–16 strings; each 1–2000 characters, not whitespace-only |
| Each schema | 32000 canonical JSON UTF-8 bytes; depth 64; 10000 nodes |
| Complete catalog | 128000 canonical JSON UTF-8 bytes |

Existing argument-size limits also apply. Definitions must be JSON-compatible;
extra definition fields, duplicate YAML mapping keys, nonfinite numbers, cyclic
aliases, invalid schemas, and unresolved references fail closed. Definition
strings are literal configuration; environment placeholders inside them are not
expanded. Catalogs are never silently truncated.

Runtime argument validation uses an isolated, terminable worker with a fixed
two-second budget covering admission, serialization, worker startup, and
validation. At most four validation workers run per service process. Cleanup
may take up to an additional 0.2 seconds. These bounds have no operator tuning
setting. Council request handling offloads catalog resolution from its async
event loop.

Exhausting the admission or validation budget, or a worker startup, crash,
protocol, or cleanup failure, rejects the request before OPA, reviewers, or writ
minting. No writ is issued. Passing validation and catalog membership do not
grant approval. Keep recursive schemas and regular expressions efficient so
legitimate requests can complete within the budget.

## OPA input

When a catalog is declared, the existing OPA input gains these fields:

| Field | Value |
| --- | --- |
| `input.council.tools` | Complete trusted definition array, including `[]` |
| `input.intent.tool_definition` | Full matched definition, or `null` |
| `input.intent.tool_arguments` | Exact JSON business argument object, or `null` |

For the request above, `tool_definition` contains all four fields of
`warehouse.delete_rows`, and `tool_arguments` is `{"table":"scratch","limit":3}`.
Shell authorize and advisory propose carry the available catalog with both
matched fields set to `null`. Omitted catalogs add none of these keys. Existing
OPA fields, including `input.intent.operation` with its digest, remain intact.
Policies must explicitly use any new restrictions they need; catalog membership
does not imply a bundled policy allow decision.

## Update and reload

Use the existing operator management path to replace the full profile, keeping
its other settings. MCP operators can use `council_put_council_profile` with
`council_id`, `management_token`, and the complete `profile` mapping; inspect it
with `council_show_council` and validate with `council_validate_catalog`.
Invalid management updates fail validation before visible configuration writes.

For a local catalog, these commands do not start a council or call an LLM:

```sh
uv run python scripts/councils.py --catalog ./local_council_catalog.yaml \
  update-from-file warehouse-review ./warehouse-review.yaml --enabled
uv run python scripts/councils.py --catalog ./local_council_catalog.yaml \
  --json show warehouse-review
uv run python scripts/councils.py --catalog ./local_council_catalog.yaml validate
```

The catalog file must already exist. In a Compose deployment, manage the mounted
runtime catalog under `deploy/dynamic-config/`; editing checked-in `configs/`
does not update an already seeded deployment. The `scripts/councils.sh` wrapper
runs management inside the council container; an update source must be readable
inside that container. See [deployment setup](../deployment-setup.md) for runtime
configuration and authentication.

New registry acquisitions reload changed catalog/profile files. Valid updates
apply to future requests; invalid replacements block new acquisitions rather
than falling back to an old permissive configuration. Removing a tool makes it
unknown for new requests. A stale catalog rejects newly introduced names until
the operator updates it. An agent cannot select an older catalog revision.

In-flight work that already acquired a service may finish using its consistent
old snapshot. Catalog edits do **not** revoke already signed writs: existing
signature, nonce, and expiry rules still apply. Plan changes around that
in-flight window and the signed writ's expiry. Writ lifetimes are selected by
risk: low 300 seconds, medium 120, high 60, and critical 30.
`default_token_ttl_seconds` controls capability/workflow timing; it does not set
these writ lifetimes. Keep definitions aligned with the actual host
implementation, and do not reuse a name for incompatible behavior while old
writs may still execute.
