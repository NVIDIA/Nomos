<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Structured tool policy and human approval

Nomos can evaluate the actual arguments of an application tool before model
review. This lets an operator distinguish a forbidden action from an action
that needs a person's approval. A human approval never overrides a forbidden
policy decision.

## What OPA receives

For a structured `tool_call`, OPA receives the validated, bounded argument
snapshot as `input.intent.tool_arguments`. The existing
`input.intent.operation` still contains the exact tool name, subject and SHA-256
argument digest. The digest binds the reviewed arguments to the signed writ;
it cannot tell a policy what those arguments mean.

For example:

```json
{
  "intent": {
    "operation": {
      "kind": "tool_call",
      "tool_name": "shell.run",
      "subject": "user-123",
      "arguments_sha256": "<digest of the exact arguments>"
    },
    "tool_arguments": {"command": "rm -rf output"}
  }
}
```

The actual arguments are available for **all structured calls**, independently
of the selected profile or an operator tool catalog. They remain untrusted
request content: presence is not schema validation against a trusted tool
definition, and arguments cannot supply policy configuration. Shell requests
retain their existing input shape.

Raw arguments are held only for the current review and are not added to the
durable Intent or signed operation. Sending them to OPA expands the data OPA
can see, however. Protect its transport, access controls and decision logs;
Nomos does not guarantee that an independently configured OPA server will not
log its input. Keep sensitive argument values out of custom policy reasons and
guidance too: those messages can be returned to clients and included in the
existing review audit.

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
