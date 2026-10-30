<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Nomos plugin third-party notices

This plugin is built from `integrations/nemo-relay` in
[NVIDIA/Nomos](https://github.com/NVIDIA/Nomos)
at the commit pinned as `source.sha` in `release.toml`.

Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.

The plugin uses Apache-2.0. The bundle keeps its original `LICENSE` and `NOTICE`
files.

The generated `ATTRIBUTIONS-Python.md` in the bundle contains license text for
the Python packages in the plugin's `uv.lock`, including test-only packages.
The runtime dependencies are `nemo-relay-plugin` (NeMo Relay 0.10.0 worker SDK),
`mcp`, `httpx`, `cryptography` and `jsonschema`, and their dependencies.

Update these notices and the root aggregate attribution files whenever you
change the source commit in `release.toml`. Packaging generates the bundle's
attribution file from that commit.
