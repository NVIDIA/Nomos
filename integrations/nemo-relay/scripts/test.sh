#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Run the tests with branch coverage of nemo_relay_nomos (make test).
#   scripts/test.sh                  every offline tier; fails under the coverage floor in pyproject.toml
#   scripts/test.sh -k citation      a subset: extra arguments go to pytest, and the floor is not applied
# The installed-worker and end-to-end tiers use the Relay CLI from the test group (.venv/bin/nemo-relay) and install
# the bundle offline from a wheelhouse made on first use (scripts/wheelhouse.sh). NOMOS_TEST_WHEELHOUSE= (empty)
# installs from the package index instead. The end-to-end tier uses the enclosing Nomos (or NOMOS_TEST_NOMOS_SOURCE).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
uvr() { env -u VIRTUAL_ENV uv run --frozen "$@"; }
export NOMOS_TEST_RELAY_BIN="${NOMOS_TEST_RELAY_BIN:-$ROOT/.venv/bin/nemo-relay}"
if [ -z "${NOMOS_TEST_WHEELHOUSE+set}" ]; then
  py="$(uvr python -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  NOMOS_TEST_WHEELHOUSE="$ROOT/.cache/wheelhouse/py$py"
  [ -d "$NOMOS_TEST_WHEELHOUSE" ] || PYTHON_VERSION="$py" WHEELHOUSE="$NOMOS_TEST_WHEELHOUSE" scripts/wheelhouse.sh
  export NOMOS_TEST_WHEELHOUSE
fi
mkdir -p .cache
cov=(--cov --cov-report=term-missing:skip-covered --cov-report=xml:.cache/coverage.xml)
[ $# -eq 0 ] || cov+=(--cov-fail-under=0)
uvr pytest "${cov[@]}" "$@"
