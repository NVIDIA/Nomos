<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Proposed upstream patches (not applied)

The plugin runs against **unmodified** NeMo Relay 0.11.x (`nemo-relay-plugin` and `nemo-relay` from PyPI,
`>=0.11.0,<0.12`) and the enclosing Nomos. The patches below were written against Relay 0.10.0 and the test bed's
Nomos (`vendor/nomos` @ `54bf5ed`); they record upstream fixes for limitations the plugin worked around then. Each
one:

- applies cleanly to the named upstream commit (`git apply --check`);
- was verified in a scratch `git worktree` of the upstream repository, never in a checked-in copy;
- is listed with the exact command and result used to verify it.

| Patch | Fixes | Status for the supported Relay range |
|---|---|---|
| `relay-codec-align-tools-by-name.patch` | Relay codecs refuse to write back more than one edited tool per request ([R13](../findings.md#r13-new-relays-codecs-cannot-patch-back-more-than-one-edited-tool-per-request)) obsolete: Relay 0.11.0 fixes R13 upstream, so no workaround is needed; kept as a record of the 0.10 fix |

## relay-codec-align-tools-by-name.patch

`crates/core/src/codec/mod.rs` gains `encode_changed_tools()`. The OpenAI Chat and Anthropic Messages codecs use it to
write back `tools`. When every function-tool name is unique on both sides and every edited tool has a same-named
original, each edit is patched onto the original entry with that name, so provider-only fields survive. In every other
case (renamed, new, provider-native or duplicate-named tools) it falls back to the existing value-aligned path unchanged,
including its refusal of ambiguous reorder-and-edit cases. Two tests are added in `openai_chat_tests.rs`.

Verification:

```text
$ cargo test -p nemo-relay --lib codec::            # in a worktree at a922697 + patch
test result: ok. 597 passed; 0 failed; 0 ignored; 0 measured; 1152 filtered out
  codec::openai_chat::tests::chat_several_tools_edited_in_place_keep_their_own_provider_fields ... ok   (new)
  codec::openai_chat::tests::chat_reordered_and_edited_tools_align_by_name ... ok                       (new)
  codec::openai_chat::tests::chat_reordered_and_edited_content_parts_are_rejected_without_provenance ... ok (unchanged safeguard)
  codec::openai_chat::tests::chat_tool_edits_preserve_nested_unknown_fields_and_explicit_nulls ... ok      (rename path)

# Python binding built from the patched worktree, same agent code, codec path, two guarded tools:
unpatched: RuntimeError: invalid argument: cannot safely preserve provider fields for multiple edited array items without stable identities
patched:   nemo_relay 0.10.0 | codec path, 2 guarded tools: run_shell requires nomos = True | read_file requires nomos = True | result: executed
```

Formatted with `rustfmt` (edition 2024). Not yet run: `cargo clippy` and the Gemini and Responses codecs, which use
their own tool patchers and are not changed.

Re-verified on 2026-10-12 against the **0.10.0 release tag** (`494018ac`), the version the plugin targets. The patch
applies cleanly (`git apply --check`), and:

```text
$ cargo test -p nemo-relay --lib codec::            # in a worktree at 494018ac + patch
test result: ok. 600 passed; 0 failed; 0 ignored; 0 measured; 1324 filtered out
  test codec::openai_chat::tests::chat_reordered_and_edited_tools_align_by_name ... ok
  test codec::openai_chat::tests::chat_several_tools_edited_in_place_keep_their_own_provider_fields ... ok
```

## Reproducing the verification

```bash
git clone https://github.com/NVIDIA/NeMo-Relay.git /tmp/nemo-relay
git -C /tmp/nemo-relay worktree add --detach /tmp/relay-fix 0.10.0
git -C /tmp/relay-fix apply "$PWD/docs/upstream/relay-codec-align-tools-by-name.patch"
(cd /tmp/relay-fix && cargo test -p nemo-relay --lib codec::)
git -C /tmp/nemo-relay worktree remove /tmp/relay-fix
```
