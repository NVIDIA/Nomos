#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Download the plugin's runtime dependencies (and its build backend) as wheels into .cache/wheelhouse/py<version>,
# so `nemo-relay plugins add` can pip-install the bundle offline:
#   PIP_NO_INDEX=1 PIP_FIND_LINKS=.cache/wheelhouse/py<version>
# Relay's `plugins add` runs `pip install <bundle>` and ignores uv.lock; pinning the versions here from the lock keeps
# test installs reproducible. The wheels must match the interpreter Relay builds the worker environment with
# (NEMO_RELAY_PYTHON; the tests pass their own), so there is one wheelhouse per Python version.
#   scripts/wheelhouse.sh            # for the project's interpreter
#   PYTHON_VERSION=3.12 scripts/wheelhouse.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_VERSION="${PYTHON_VERSION:-$(env -u VIRTUAL_ENV uv run --project "$ROOT" --frozen python -c 'import sys; print("%d.%d" % sys.version_info[:2])')}"
WHEELHOUSE="${WHEELHOUSE:-$ROOT/.cache/wheelhouse/py$PYTHON_VERSION}"
mkdir -p "$WHEELHOUSE"
reqs="$(mktemp)"
trap 'rm -f "$reqs"' EXIT
env -u VIRTUAL_ENV uv export --project "$ROOT" --frozen --no-dev --no-emit-project --no-hashes -q > "$reqs"
echo "setuptools>=77" >> "$reqs"
echo "==> downloading (Python $PYTHON_VERSION) $(grep -c '==' "$reqs") pinned wheels into $WHEELHOUSE"
env -u VIRTUAL_ENV uv run --no-project --python "$PYTHON_VERSION" --with pip python -m pip download -q \
  --only-binary=:all: -r "$reqs" -d "$WHEELHOUSE"
echo "$WHEELHOUSE"
