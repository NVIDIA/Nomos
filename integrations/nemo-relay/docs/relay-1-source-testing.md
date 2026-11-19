<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Relay 1.0 source-build testing

This draft requires Relay 1.0's `nvidia.nemo.relay.worker.v1` worker protocol.
The Python runtime and worker SDK are pinned in `pyproject.toml` and `uv.lock`
to public commit `101e1e9ed43175b80cfd253c9ed3ba37b9395f18` from
[Relay PR #1749](https://github.com/NVIDIA/NeMo-Relay/pull/1749).
The manifest accepts Relay `>=1.0.0,<1.1`; Relay 0.11 installations are not compatible.

These are source-build instructions, not verification of a published release.
The CLI package is deliberately not resolved from `python/cli-bin` in Git: that
project is a development placeholder, not the platform-specific executable wheel.
Build the CLI from the same commit and provide its path explicitly.

## Prerequisites

Use Python 3.11+, Rust/Cargo and the tools required by the pinned Relay source.
In particular, install Buf 1.75.0 (the pinned CI version) and protobuf standard
include files. Set `BUF` if Buf is not on PATH and `PROTOC_INCLUDE` to the directory
containing `google/protobuf/descriptor.proto`. Building from a warm cache can hide
these prerequisites; a fresh source checkout needs them too.

Clone the public Relay source separately, then check out the exact revision:

```bash
git clone https://github.com/nathankw/NeMo-Relay.git relay-source
git -C relay-source checkout 101e1e9ed43175b80cfd253c9ed3ba37b9395f18
export RELAY_SOURCE="$(cd relay-source && pwd)"
```

## Build and install

Run the following from this integration's directory. Source-built wheels and
executables stay outside tracked configuration; do not commit local paths or
claim a source-build run proves that released packages are compatible.

```bash
export NOMOS_TEST_RELAY_BIN="$RELAY_SOURCE/target/debug/nemo-relay"
export RELAY_WHEELS="$PWD/.cache/relay-source-wheels"
mkdir -p "$RELAY_WHEELS"

# These use Relay's normal Cargo and PEP 517 build entry points.
(cd "$RELAY_SOURCE" && cargo build --locked -p nemo-relay-cli --features __skip-implicit-config)
uv build --wheel --out-dir "$RELAY_WHEELS" "$RELAY_SOURCE/python/plugin"

# The lock uses the pinned public Git sources, not a workstation directory.
uv sync --frozen

# pip in Relay's fresh worker environment does not read uv source overrides.
# Supply the actual matching SDK wheel and locked runtime dependencies offline.
export NOMOS_TEST_RELAY_SDK_WHEEL="$RELAY_WHEELS/nemo_relay_plugin-1.0.0-py3-none-any.whl"
scripts/wheelhouse.sh
make test
make lint
```

`NOMOS_TEST_RELAY_BIN` is required during this draft. Runtime and CLI version
strings alone cannot prove that two development builds implement the same
contract; build both from the pinned source and retain build/test evidence.
The worker installer must validate the unchanged integrity and compatibility
checks. Do not widen the old protocol label or disable those checks to load it.

## Required acceptance coverage

The managed-worker tests install into a fresh isolated environment through the
real CLI. Verify ordinary native request correlation and streaming as well as
`execution_context.data.host_approval` over the remote callback channel. Cover
valid approval, invalid/untrusted assertions, changed arguments, approval reuse,
continued downstream middleware, and current-policy denial after human approval.
A passing in-process interceptor test does not establish managed-worker transport.

The optional combined core-policy tier needs the core service's dependencies as
well as the worker's. Run it with `uv run --frozen --with
"$NOMOS_TEST_NOMOS_SOURCE" pytest -q tests/test_core_policy_runtime.py` after
setting the variables in [host approval](host-human-approval.md#validation).
This uses a temporary dependency overlay; it does not change the worker lock.

Before release, replace the draft Git source overrides with published packages,
restore the matching `nemo-relay-cli-bin` test dependency, regenerate the lock,
and rerun clean installation and the managed-worker tests. Keep the feature draft
until those released-package checks pass.
