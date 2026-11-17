<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Structured tool policy and human approval

Nomos can evaluate the actual arguments of an application tool before model
review. This lets an operator distinguish a forbidden action from an action
that needs a person's approval. A human approval never overrides a forbidden
policy decision.

<a id="structured-tools-versus-legacy-shell-authorization"></a>

## Two execution paths

Each Relay tool's `executor` selects where an approved operation runs and which
authorization format Nomos uses. There is no separate `legacy = true` switch.

| Question | `executor = "local"` | `executor = "nomos_http"` |
| --- | --- | --- |
| Who executes after approval? | The application, through Relay's callback. | The trusted Nomos Executor service. |
| What does Nomos authorize? | A named tool with its exact JSON arguments (`tool_call`). | A shell command with its working directory. |

Set this in **Relay's `plugins.toml`**, inside the Nomos plugin's existing
`[[plugins.dynamic]]` entry:

```toml
# Explicit execution route for this exact tool.
[plugins.dynamic.config.tools."shell.run"]
executor = "local"

# Default for guarded tools without an explicit per-tool entry.
[plugins.dynamic.config]
default_executor = "local"
```

`local` is already the default for unlisted guarded tools. Explicit per-tool
entries take precedence, including the shipped `run_shell` entry that uses
`nomos_http` when the `tools` configuration is omitted. These are Relay plugin
settings; the Council's OPA profile is configured separately in YAML below.

Both paths begin with JSON tool arguments in Relay. Here, **structured** means
Nomos's `tool_call` authorization format. `local` means the application's
callback, which may run on a workstation or server.

## Example OPA inputs: the same command, two paths

Both examples propose `/bin/echo hello` with working directory
`/workspace/project`. They show abbreviated OPA HTTP request bodies formatted
as YAML for readability; the wire format is JSON. Other identity, authentication
and review metadata is omitted. The outer `input` is OPA's HTTP wrapper: Rego
accesses the contents as `input.intent`, not `input.input.intent`.

### `executor = "local"`: arguments stay inside the tool call

This example uses `opa.policy_id: local-bash` with an explicit
`shell.run: command` binding. Nomos supplies the exact tool arguments and derives
shell facts for that bound command:

```yaml
input:
  council:
    opa_policy_id: local-bash
  intent:
    command_type: api
    command: ""
    working_directory: ""
    shell_command_words: []
    operation:
      kind: tool_call
      schema_version: 1
      tool_name: shell.run
      subject: user-123
      arguments_sha256: "<SHA-256 of the exact tool arguments>"
      canonicalization: nomos-json-v1
    tool_arguments:
      command: /bin/echo hello
      cwd: /workspace/project
    shell_analysis:
      supported: true
      commands:
        - name: echo
          argv: [hello]
      reason: ""
```

The proposed command is in **`tool_arguments.command`**. The legacy
`intent.command` remains empty. The `local-bash` rules use `shell_analysis`;
older rules that inspect only `intent.command` do not inspect the nested command.
The argument digest binds the reviewed arguments to a signed writ; it does not
tell policy what the command means.

**Why is `tool_arguments` beside `operation`, rather than inside it?**
[`ToolCallOperation`](../../nomos/level_0/types/tool_call.py) is the compact
operation record persisted in the Intent and bound into the signed writ,
introduced by [PR #112](https://github.com/NVIDIA/Nomos/pull/112). It carries the
argument digest rather than the raw arguments. Adding raw arguments inside
that record would require changing its schema and reviewing the persistence,
privacy and signing implications. The sibling `intent.tool_arguments` field
gives OPA the actual arguments during review while preserving that contract.
Both come from the same validated request; the digest is calculated from those
exact arguments, not supplied independently by the model.

`operation.kind` identifies the operation type. Its only currently supported
value is `"tool_call"`; legacy shell requests use `operation: null`, not
`kind: shell`. The kind does not identify the tool or its executor: `tool_name`
names the tool, and the Relay configuration selects its execution route.

### `executor = "nomos_http"`: command uses the legacy shell fields

This example uses the existing `strict-shell` policy profile:

```yaml
input:
  council:
    opa_policy_id: strict-shell
  intent:
    command_type: shell
    command: /bin/echo hello
    working_directory: /workspace/project
    shell_command_words: [/bin/echo]
    operation: null
```

Here, the command is in **`intent.command`**, which the existing shell rules
inspect. `tool_arguments` and `shell_analysis` are absent, not empty objects.
An approved shell writ goes to the trusted Nomos Executor.

**Policy selection is separate from executor selection.** The examples use
different operator-configured profiles; setting `executor` does not select a
profile automatically. Structured calls always supply their arguments to OPA,
but `shell_analysis` is added only for a bound tool under `local-bash`. Selecting
`local-bash` does not make its new command rules analyze legacy `nomos_http`
requests.

<details>
<summary>More detail: defaults and field-by-field reference</summary>

Unlisted guarded tools use `default_executor`, whose default is `local`.
There is one shipped explicit exception: omitting the `tools` configuration
retains the built-in `run_shell` entry with `executor = "nomos_http"`.
See the [Relay plugin configuration](../../integrations/nemo-relay/README.md).
Direct Council API clients select the format by supplying either `tool_call`
or the top-level shell fields. Combining a `tool_call` with nonempty shell
execution fields is rejected, rather than silently ignoring conflicting values.

The table below compares the **two YAML examples above**, both proposing
`/bin/echo hello`. All field paths start inside `input.intent`.
**Not present** means the key is absent; `""`, `[]` and `null` are actual values
sent to OPA.

| Field inside `input.intent` | `local` example | `nomos_http` example |
| --- | --- | --- |
| `command_type` | `"api"` | `"shell"` |
| `tool_arguments.command` | `"/bin/echo hello"` | Not present |
| `tool_arguments.cwd` | `"/workspace/project"` | Not present |
| `command` | `""` | `"/bin/echo hello"` |
| `working_directory` | `""` | `"/workspace/project"` |
| `operation` | Tool-call binding object shown above | `null` |
| `shell_command_words` | `[]` | `["/bin/echo"]` |
| `shell_analysis` | Derived command facts shown above | Not present |

The important difference is **where the command and working directory live**.
For `local`, read them from `tool_arguments`; for `nomos_http`, read the
top-level shell fields. The `cwd` argument is specific to this example tool;
other tools can define different arguments.

**What this means for policy:** OPA does not automatically ignore or translate
fields. Each Rego rule reads the fields it names:

- Existing shell rules, such as absolute-executable-path and
  remote-fetch-to-interpreter checks, use the top-level shell fields. They do
  not automatically inspect the command inside `tool_arguments`.
- The new `local-bash` command rules use `shell_analysis`, which is supplied
  only for an exactly bound structured tool under that profile.
- Existing sensitive-path checks use `normalized_path_refs`, derived from the
  top-level command, working directory and target resource. Paths nested inside
  tool arguments are not automatically included.

Identity, permissions, review context, justification and other shared metadata
remain available on both paths. Both examples have `packaged_files: []` (not
shown in the YAML excerpts); legacy shell requests may also supply packaged-file
metadata, whereas structured authorization rejects packaged-file execution
fields.

See the [OPA input builder](../../nomos/opa/client.py) and the
[bundled Rego rules](../../policies/intent_filter.rego) for the implementation.

</details>

### Argument privacy

Actual tool arguments remain untrusted request content, not trusted tool
definitions or policy configuration. They are available to OPA independently
of any operator tool catalog. Raw arguments are held for the current review
and are not added to the durable Intent or signed operation.

OPA can now see those arguments, so protect its transport, access and decision
logs. Nomos does not control an independently configured OPA server's logging.
Keep sensitive values out of custom policy reasons and guidance too: those
messages can reach clients and the review audit.

## Opt in to the local Bash profile

Existing profiles keep their existing rules. Enable this profile explicitly
and bind each exact tool name to the argument containing its command:

```yaml
opa:
  enabled: true
  policy_id: local-bash
  shell_tool_arguments:
    shell.run: command
```

An empty binding map is rejected for this profile. Unbound tools continue
through ordinary policy and Council review; the profile does not infer a tool's
meaning from its name or description. The application must still enforce its
own tool allowlist, user consent, folder grants and filesystem confinement.

The server derives `input.intent.shell_analysis` from the held command. The
bundled Rego policy consumes these facts, rather than trusting model-provided
command classifications.

| Recognized command | Policy result |
| --- | --- |
| Recursive forced `rm`, including `-rf`, `-fr`, `-r -f` and long options | Forbidden |
| `sudo` | Forbidden |
| Login `su`, including `-`, `-l`, combined `-lc`, and `--login` | Forbidden |
| `chmod` setting numeric mode `777` or `0777` | Forbidden |
| Other `rm` or `su` | Human approval required |
| Unsupported or opaque shell syntax | Human approval required |
| Other recognized literal commands | Continue to Council review |

This is a conservative **static grammar**, not a Bash interpreter or a sandbox.
It recognizes one simple command with literal arguments and a bounded set of
standard command names. Expansion, globbing, composition, redirection,
interpreters, unfamiliar executables and malformed input require human review.
Some harmless quoted metacharacters consequently require review too.

Hard-deny rules take precedence over human-review rules for recognized
commands. A proven literal executable/option prefix is retained for denial
only: `rm -rf *` and `rm -rf "$TARGET"` are forbidden even though their targets
need expansion. This prefix is never sufficient to allow a command. An opaque script can contain operations the static grammar does not
recognize; human approval accepts responsibility for that opaque operation.
The profile does not promise to prove every possible Bash effect safe or to
detect a prohibited operation hidden in an arbitrary program. Runtime isolation
and host environment integrity, including inherited shell functions, remain
separate controls.

## Three policy decisions

OPA may return an explicit `policy_decision` alongside its existing boolean
`allow`. Contradictory pairs are rejected:

| `policy_decision` | `allow` | Meaning |
| --- | --- | --- |
| `allow` | `true` | Continue through normal model review; not permission to execute yet. |
| `forbidden` | `false` | Block; a person cannot override this policy through the approval replay path. |
| `requires_human_approval` | `false` | Stop before execution and let the application obtain approval. |

Normal authorization keeps the existing `status: denied` for both forbidden
and human-required results and adds the typed `policy_decision` field. Older
clients remain closed. Applications must inspect the field, not parse guidance
or error text to decide whether to show an approval card. Neither an outage nor
an undefined decision means that human approval is required.

## Recheck policy after approval

An application with its own approval interface can recheck current policy
without repeating model review. Here `council.authorize` represents an
application-owned client wrapper that calls the `council_authorize` MCP tool;
it is not a new Nomos Python SDK method:

```python
result = await council.authorize(
    api_version="v2",
    council_id="operations",
    agent_id="example-application",
    auth_token=agent_token,
    tool_call=exact_tool_call,
    policy_only=True,
)
```

This is an inspection request, not proof that a person approved anything. It
authenticates and checks permissions as usual, evaluates OPA and stops before
Council review or writ minting. A successful inspection returns
`status: policy_allowed`, `policy_decision: allow`, and no writ. A human-required
inspection returns `status: denied` and `policy_decision:
requires_human_approval`; a forbidden inspection retains `status: denied` and
returns `policy_decision: forbidden` instead.

An integrating worker may continue only after independently verifying an
application-owned approval bound to the exact final operation, and only when
current policy allows it or requires that human approval. It must still block
forbidden actions, missing or invalid approvals, and unavailable policy.
An inspection response by itself must never authorize a tool effect.

Policy-only requests require enabled, healthy OPA even when ordinary
authorization is configured to fail open. They never mint a writ and are
recorded as policy inspection in the existing consolidated review ledger,
including installations using the legacy review-audit mode. The opt-in
`local-bash` profile likewise records its pre-review denials and human-review
requirements in both audit modes, with `request_type: authorize` for ordinary
authorization requests. These records contain the privacy-minimized operation,
policy outcome and reason, not fabricated Council votes or authorization.

## Validation

Run the Python contracts and the actual Rego engine together:

```sh
NOMOS_TEST_OPA_BINARY=/path/to/opa python -m pytest \
  tests/test_opa_tool_policy.py tests/test_opa_client.py \
  tests/test_opa_nat_integration.py
opa test policies/intent_filter.rego policies/intent_filter_test.rego
```

Without `NOMOS_TEST_OPA_BINARY`, Python tests requiring the external Rego engine
are explicitly skipped. The bundled policy and its Kubernetes chart copy must
remain identical.
