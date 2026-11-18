<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Structured tool policy and human approval

Nomos can evaluate the actual arguments of an application tool before model
review. This lets an operator distinguish a forbidden action from an action
that needs a person's approval. A human approval never overrides a forbidden
policy decision.

For the opt-in `local-bash` profile, see the [default command-policy
table](#default-local-bash-command-rules) for commands that are blocked or require
human approval. The shipped Council configuration does not enable this profile
by default.

<a id="structured-tools-versus-legacy-shell-authorization"></a>

## Two execution paths

Each Relay tool's `executor` selects where an approved operation runs and which
authorization format Nomos uses. There is no separate `legacy = true` switch.

| Question | `executor = "local"` | `executor = "nomos_http"` |
| --- | --- | --- |
| Who executes after approval? | The application, through Relay's callback. | The trusted Nomos Executor service. |
| What does Nomos authorize? | A named tool with its exact JSON arguments (`tool_call`). | A shell command with its working directory. |

Set this in **Relay's `plugins.toml`**, inside the Nomos plugin's existing
`[[plugins.dynamic]]` entry. Merge these settings into existing tables; do not
append duplicate TOML table headers. Add `shell.run` to the existing
`guard.include` list, preserving the other guarded tools, and ensure no
`guard.exclude` pattern matches it. An executor entry alone does not guard a tool.

When adding a `tools` map, preserve every existing tool route: an explicit map
replaces the built-in map. For example, keep the shipped `run_shell` route to the
trusted Executor alongside the new application-owned `shell.run` route:

```toml
# Example additions to the existing guard (retain other includes/excludes).
[plugins.dynamic.config.guard]
include = ["run_shell", "shell.run"]

# Explicit execution route for this exact tool.
[plugins.dynamic.config.tools."shell.run"]
executor = "local"

# Preserve the existing trusted-Executor route (including any custom fields).
[plugins.dynamic.config.tools."run_shell"]
executor = "nomos_http"
command_argument = "command"
```

`local` is already the default for unlisted guarded tools. If setting it
explicitly, add just this key to the **existing** `[plugins.dynamic.config]`
table, before any of its nested table headers:

```toml
default_executor = "local"
```

Explicit per-tool entries take precedence. The shipped `run_shell` entry uses
`nomos_http` when the `tools` configuration is omitted; keep that route unless
you intend to change who executes it. These are Relay plugin settings; the
Council's OPA profile is configured separately in YAML below.

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
      fully_parsed: true
      commands:
        - executable: /bin/echo
          name: echo
          argv: [hello]
      reason: ""
```

The proposed command is in **`tool_arguments.command`**. The legacy
`intent.command` remains empty. The `local-bash` rules use `shell_analysis`;
older rules that inspect only `intent.command` do not inspect the nested command.
The argument digest binds the reviewed arguments to a signed writ; it does not
tell policy what the command means.

`fully_parsed` means the analyzer can describe the entire command's **syntax**
within its grammar and resource limits, not that
the executable is trusted or the operation is approved. Each command fact keeps
the exact `executable` spelling, its basename as `name`, and its arguments as
`argv`. For example, `./build/test --list` produces `executable: ./build/test`,
`name: test` and `argv: [--list]`; it does not require a recognized-command list.

An incomplete analysis can still retain literal command facts. For example,
`rm -rf "$TARGET"` produces:

```yaml
fully_parsed: false
commands:
  - executable: rm
    name: rm
    argv: [-rf]
reason: "Variable-expansion syntax ($NAME or ${NAME}) cannot be fully analyzed."
```

The reason describes the analysis limitation without repeating argument values.
It does not determine whether a human is required. For `cat "$TARGET"`, OPA
continues to normal Council review of the full proposed command. For
`rm "$TARGET"`, the explicit deletion rule requires human approval regardless
of the variable; replacing it with a literal does not waive that requirement.
For `rm -rf "$TARGET"`, the known prefix is forbidden. A parsing limitation
never excuses a known prohibition. Malformed quotes, size limits, composition
and other syntax limits retain distinct diagnostic explanations.

`fully_parsed` replaces the draft field `supported` as a diagnostic. Neither
`false` nor a missing completeness flag automatically requests HITL; explicit
policy rules decide between denial, mandatory human review and further Council
review. Conservative analysis may flag quoted literal metacharacters because
it does not retain quote provenance. Applications must use the authorization
response `status`, not explanation text or parse completeness, for routing.

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
  denied_shell_commands:
    - restricted-helper
```

An empty binding map is rejected for this profile. Unbound tools continue
through ordinary policy and Council review; the profile does not infer a tool's
meaning from its name or description. The application must still enforce its
own tool allowlist, user consent, folder grants and filesystem confinement.

The server derives `input.intent.shell_analysis` from the held command. The
bundled Rego policy consumes these facts, rather than trusting model-provided
command classifications.

`opa.denied_shell_commands` is an optional deployment-owned list of executable
basenames to forbid before Council review. It defaults to `[]`; leaving it
empty retains the bundled rules below. Entries are exact, case-sensitive names,
not paths or wildcard patterns. In this synthetic example, `restricted-helper`,
`./build/restricted-helper` and `/workspace/project/build/restricted-helper`
all match the configured name; `restricted-helper-extra` does not.

The Council sends this configuration to OPA as
`input.council.opa_denied_shell_commands`, separately from tool arguments.
A model-supplied argument cannot replace the configured list. A matching
prohibition is a hard denial, even if another rule requests human review;
human approval cannot override it on replay.

Operators should supply their own reviewed command baseline through deployment
configuration. The bundled rules and synthetic example are a starting profile,
not a complete organizational security policy. Matching depends on the static
command facts described below; it does not discover commands hidden inside
scripts or establish the identity of an executable's contents.

### Default local Bash command rules

These are the bundled `local-bash` rules in
[`policies/intent_filter.rego`](../../policies/intent_filter.rego). They apply
after the profile is explicitly enabled and the tool is bound.

| Proposed command or syntax | Policy result |
| --- | --- |
| Executable basename listed in `opa.denied_shell_commands` | Forbidden |
| Recursive forced `rm`, including `-rf`, `-fr`, `-r -f` and long options | Forbidden |
| `sudo` | Forbidden |
| Login `su`, including `-`, `-l`, combined `-lc`, and `--login` | Forbidden |
| `chmod` setting numeric mode `777` or `0777` | Forbidden |
| Other `rm` or `su` | Human approval required |
| Dispatch wrappers such as `env`, `exec`, `command` or `eval`, and shell inline code such as `bash -lc 'id'` | Continue to Council review unless an explicit rule matches known facts |
| Unsupported or dynamic shell syntax, including variable expansion and bare `time` | Continue to Council review unless an explicit rule matches known facts |
| Other literal invocations, including `git status`, `python3 script.py` and `./build/test --list` | Continue to Council review |

This is a conservative **static grammar**, not a Bash interpreter or a sandbox.
It describes one simple command with a literal executable and arguments. It
does not restrict executable names or directories: bare names (`git`), absolute
paths (`/workspace/project/build/test`) and relative paths (`./build/test`) can
all be analyzed. An absolute path is not required. Relative paths are resolved
by the host against the actual working directory; bare names use the host's
command lookup. The analyzer does not resolve those paths or inspect executable
contents. Expansion, globbing, composition, redirection and malformed syntax
can produce `fully_parsed: false`. This describes analysis coverage; it does
not impose human approval. Some harmless quoted metacharacters also produce
incomplete analysis. The full proposed command still goes to Council review
when no explicit denial or human-review rule applies.

The supplied Rego rules make the command-policy decisions. They match the
parsed basename, so `/workspace/project/build/rm -rf output` is subject to the
same recursive forced-deletion rule as `/bin/rm -rf output`. This is a
conservative name-based policy, not proof that two executables have identical
behavior. A custom program, wrapper or renamed binary can perform effects its
name does not reveal; a full path alone does not establish executable trust.

Hard-deny rules take precedence over human-review rules. A proven literal
executable/option prefix remains evidence for explicit policy rules:
`rm -rf *` and `rm -rf "$TARGET"` are forbidden even though their targets need
expansion, while `rm "$TARGET"` requires human approval.
Completed words survive a later parse error or analysis limit; unfinished words
are not treated as executable names or options. A retained prefix is never
sufficient to authorize execution.

Wrappers and shell inline-code invocations do not automatically require HITL.
For example, `env sudo id` has outer executable `env`, while
`bash -lc 'rm -rf output'` has outer executable `bash`. The analyzer does not
recursively analyze the nested operation, so it cannot claim that the inner
command was matched against the deny or human-review rules. Council reviews
the full proposed command. This profile does not establish that arbitrary
scripts or program effects are safe, and allowing further review does not
authorize execution.

The bundled mandatory-human rules cover identified `rm` and non-login `su`
invocations; hard denials take precedence. There is currently no `git push`
human-review rule in this profile. The table describes the implemented rules,
not complete coverage of every deployment's required ask/deny list.

Executable spelling is separate from the working directory and filesystem
grants. The host still validates the actual working directory, permitted paths
and runtime confinement. An analyzer accepting a path does not grant access to
it or widen the host's filesystem permissions. The profile does not promise to
detect every prohibited effect inside an arbitrary program; runtime isolation
and host environment integrity, including inherited shell functions, remain
separate controls.

## Policy decisions and authorization status

OPA returns a `decision` describing policy. The authorization API returns a
`status` describing what the application should do next. These are separate
boundaries: an OPA allow continues normal Council review; it does not authorize
execution by itself.

| OPA `decision` | Normal `council_authorize` response | Application action |
| --- | --- | --- |
| `allow` | Continue Council review; `approved` with a writ only if authorized. | Execute only after validating the writ. |
| `deny` | `status: denied`, no writ. | Block; human approval cannot override policy. |
| `requires_human_approval` | `status: requires_human_approval`, no writ. | Obtain application-owned human approval before proceeding. |

The built-in OPA document also exposes the legacy `allow` boolean (`true` only
for `decision: allow`). Custom policies can return `decision` alone, a boolean,
or a legacy boolean-only `allow`/`allowed` object. If both decision and boolean
are supplied, they must agree. Invalid or contradictory responses are evaluation
errors. The obsolete `policy_decision` field is rejected rather than translated
into a human-approval request.

At the public authorization boundary, **`status` is the sole outcome field**:
`approved`, `denied`, `requires_human_approval`, `policy_allowed`, or `error`.
Applications must never infer approval requirements from prose, errors or other
fields. Only `requires_human_approval` requests a human. Infrastructure failures
and ordinary Council denials never become approval cards.

For example, `rm "$TARGET"` requires a human because of the explicit deletion
rule, not because the variable is unresolved. Selected response fields are shown
below with illustrative IDs:

```json
{
  "status": "requires_human_approval",
  "decision_id": "example-human-review",
  "writ": null,
  "guidance": [
    "This operation requires human approval. Do not execute before approval."
  ],
  "error": "Human approval required by OPA policy: Deletion requires human approval."
}
```

`rm -rf "$TARGET"` is different: the literal `rm -rf` prefix already proves
this profile's prohibition, even though the target variable is unresolved.
Hard denial wins over the incomplete analysis. Its response instead looks like
this (selected fields, illustrative ID and guidance ordering):

```json
{
  "status": "denied",
  "decision_id": "example-policy-denial",
  "writ": null,
  "guidance": [
    "Safer revisions within the user's authorized goal require fresh authorization. Do not invent permissions or justification, or repeat unchanged policy-denied requests. If no compliant alternative exists, stop and ask for clarification.",
    "This operation is forbidden. Do not retry it or seek human approval to override this denial."
  ],
  "error": "Denied by OPA policy: Recursive forced deletion is forbidden.; Deletion requires human approval."
}
```

Denial responses prepend general guidance about fresh authorization for safer
revisions; a human-required response does not prepend that denial advice.
Neither response permits execution. These statuses belong to
`council_authorize`; the separate proposal/workflow API retains its existing
workflow states and does not provide this host approval protocol.

**Migration:** update consumers to recognize the explicit human-required status
before enabling this flow. Older clients must continue to execute only explicit
`approved` responses with valid writs. Do not translate an old service's
`status: denied` plus a former `policy_decision` field into human approval:
`denied` remains denied. Update the OPA document and its parser together;
obsolete discriminator payloads are rejected, while legacy boolean policies
remain supported.

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
`status: policy_allowed` and no writ. A human-required inspection returns
`status: requires_human_approval`; a forbidden inspection returns
`status: denied`. Disabled, undefined or unhealthy OPA returns `status: error`.
Inspection never returns `approved`. A caller must reject an unexpected
`approved` response, including one from an older service that ignored
`policy_only`, rather than treating it as a successful policy recheck.

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
