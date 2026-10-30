<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Nomos plugin

## Summary

This plugin puts an application's tool calls under the oversight of a
[Nomos](https://github.com/NVIDIA/Nomos) council. It works with any
application that sends model calls through Relay's `llm.execute` and tool
calls through `tools.execute`. The application does not need any Nomos code.

The plugin runs as a separate Python program next to Relay, called a worker.
For each tool that the operator marks as guarded, it does four things:

1. It makes the model write a short statement of intent (an "intent") before
   it can call the tool.
2. It asks the council to approve that intent.
3. If the council approves, it runs the call through the configured executor.
   If not, it returns the council's full answer as the tool result.
4. If the operator gives it a signing key, it stores every answer as signed
   evidence, in the worker's memory. A later call can cite that evidence, so
   the council sees what really happened before. Without a key it keeps no
   evidence at all.

**Load and enable.** The bundled `relay-plugin.toml` registers
`nomos.council`. The manifest starts disabled. Register it, write its
configuration in `plugins.toml`, then enable it:

```sh
nemo-relay plugins add --user ./nomos-plugin/relay-plugin.toml
nemo-relay plugins enable nomos.council
```

The configuration names the council and executor, the agent's identity, the
guarded tools, the public keys the plugin trusts, and (optionally) the key the
plugin signs evidence with.
Secrets go in files that only the worker's user can read; the configuration
points at them. Bundles are not signed, so set the plugin's policy to
`attestation = "integrity_only"` for a bundle you trust, and
`startup = "required"` so Relay never runs without it. See the
[upstream plugin documentation](https://github.com/NVIDIA/Nomos/tree/main/integrations/nemo-relay#operator-configuration)
for every field and a complete example.

**Distributed artifacts.** A release has one `.tar.gz` archive for each
supported platform (Linux x86_64, Linux ARM64 and macOS ARM64), each with a
`.sha256` checksum and a `.json` build record. The archive holds the Python
package, its locked dependency list, the runtime manifest, the configuration
schema, license notices and Python dependency attributions.

## Build and test

This folder builds and releases the plugin. Its code lives in the source
repository named in `release.toml`, which also pins the exact commit.

From this repository's root:

```sh
uv run --locked python -m scripts.plugins run nomos-plugin
```

This command installs the plugin's locked environment and runs its tests. The
tests include an acceptance test that installs the plugin through the Relay
command-line tool and checks a governed tool call from start to finish against
local test services. Then it packages a bundle, extracts and installs it, sends
a model request through Relay, and checks that the guarded tool now requires an
intent. It also checks shutdown and removal, and that Relay rejects changed files.

To use newer plugin code, update `source.sha` to the commit you want. Set
`source.ref` to a branch name or to `refs/tags/<tag>`. Planning checks that a
tag resolves to the pinned commit. The ref records what to track; it does not
update the source on its own. Update `THIRD_PARTY_NOTICES.md` and the root
attribution files whenever you change the source commit.

The release version here can differ from the source package's version. A
`[relay]` setting changes only the Relay host used for tests.
