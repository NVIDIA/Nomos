# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

UV := env -u VIRTUAL_ENV uv
.DEFAULT_GOAL := help
.PHONY: help sync test test-live lint docs build wheelhouse clean

help: ## list the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-11s %s\n", $$1, $$2}'

sync: ## create .venv from uv.lock (test and lint groups)
	$(UV) sync --frozen

test: ## every offline tier, with branch coverage and its floor
	scripts/test.sh

test-live: ## the live tier: NOMOS_TEST_LIVE_CONFIG=<operator config> make test-live
	@test -n "$$NOMOS_TEST_LIVE_CONFIG" || { echo "set NOMOS_TEST_LIVE_CONFIG to an operator config (see tests/test_live_worker.py)"; exit 2; }
	scripts/test.sh tests/test_live_contract.py tests/test_live_worker.py

lint: ## every pre-commit hook of this integration over its files (run from Nomos's root; see .pre-commit-config.yaml)
	$(UV) run --frozen pre-commit run --config .pre-commit-config.yaml --all-files

docs: ## redraw docs/figures and re-render docs/architecture
	$(UV) run --frozen --group docs python docs/build/figures.py
	$(UV) run --frozen python docs/build/architecture.py

build: ## sdist and wheel into dist/
	$(UV) build --out-dir dist

wheelhouse: ## pinned wheels for offline bundle installs (.cache/wheelhouse/py<version>)
	scripts/wheelhouse.sh

clean: ## remove build and test output
	rm -rf dist build nemo_relay_nomos.egg-info .cache .coverage .pytest_cache .mypy_cache .ruff_cache
