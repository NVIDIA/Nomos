<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Recorded examples

The JSON files here are real payloads, recorded by the integration test bed (`nomos_relay_playground`,
`docs/build/capture_examples.py`), which drives this plugin through an unchanged agent and the real Relay host:

| Mark | Recorded from |
|---|---|
| 🧪 offline | the agent, the real Relay host and the packaged worker, against loopback Nomos services that speak the real wire contract; a capture-only worker entry dumps the plugin's in-memory tables after every call |
| 🌐 live | the same scripted calls against a Nomos deployment (an Astra Council and an OpenShell Executor): what a host can see, including real member votes |

Tokens are replaced by `<redacted>`, and the recording host's file paths by `/opt/relay/…` and
`/run/secrets/nomos/…`; the keys are test keys. [`../../build/architecture.py`](../../build/architecture.py)
renders the pages in [`docs/architecture/`](../README.md) from these files and from the current source, and fails on
any path, symbol or column the files or the code do not have. Re-record in the test bed after a change to the wire
format or the store's tables, then copy the files here and run `make docs`.
