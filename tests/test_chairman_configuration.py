# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Independent acceptance cases derived from architect/plan.json, never candidate source.

Run against a checkout selected by PYTHONPATH; see qa/report-qa.md. No live services.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

import nomos.config.settings as settings_module
from nomos.config.settings import (
    ChairmanConfig,
    CouncilSettings,
    load_config,
    load_council_catalog,
)


@pytest.fixture(autouse=True)
def isolated_development_environment(monkeypatch):
    """Use actual loaders/preflight with deterministic defaults, without credentials."""
    for key in tuple(os.environ):
        if key.startswith(("COUNCIL_", "NOMOS_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NOMOS_DEPLOY_PROFILE", "development")


def construct_chairman(given):
    return ChairmanConfig(**given)


def validate_chairman(given):
    return ChairmanConfig.model_validate(given)


def validate_nested_chairman(given):
    return CouncilSettings.model_validate({"chairman": given}).chairman


MODEL_ENTRY_POINTS = [
    pytest.param(construct_chairman, id="constructor"),
    pytest.param(validate_chairman, id="model-validate"),
    pytest.param(validate_nested_chairman, id="nested-council"),
]
LOADERS = [
    pytest.param(load_config, id="load-config"),
    pytest.param(load_council_catalog, id="load-council-catalog"),
]
VALID_INPUTS = [
    pytest.param({}, (False, False), id="empty-defaults"),
    pytest.param({"enabled": False}, (False, False), id="disabled-omitted-quorum"),
    pytest.param({"enabled": True}, (True, False), id="enabled-omitted-quorum"),
    pytest.param(
        {"enabled": False, "allow_quorum": False}, (False, False), id="disabled-unanimous"
    ),
    pytest.param({"enabled": True, "allow_quorum": False}, (True, False), id="enabled-unanimous"),
    pytest.param({"enabled": True, "allow_quorum": True}, (True, True), id="enabled-quorum"),
    pytest.param(
        {"enabled": "false", "allow_quorum": "false"}, (False, False), id="string-disabled"
    ),
    pytest.param(
        {"enabled": "true", "allow_quorum": "false"}, (True, False), id="string-unanimous"
    ),
    pytest.param({"enabled": "true", "allow_quorum": "true"}, (True, True), id="string-quorum"),
    pytest.param({"enabled": 0, "allow_quorum": 0}, (False, False), id="integer-disabled"),
    pytest.param({"enabled": 1, "allow_quorum": 1}, (True, True), id="integer-quorum"),
]
INVALID_INPUTS = [
    pytest.param({"enabled": False, "allow_quorum": True}, id="disabled-quorum"),
    pytest.param({"allow_quorum": True}, id="omitted-enabled-quorum"),
    pytest.param({"enabled": "false", "allow_quorum": "true"}, id="string-disabled-quorum"),
    pytest.param({"enabled": 0, "allow_quorum": 1}, id="integer-disabled-quorum"),
    pytest.param({"enabled": "off", "allow_quorum": "on"}, id="on-off-disabled-quorum"),
    pytest.param({"enabled": "no", "allow_quorum": "yes"}, id="yes-no-disabled-quorum"),
]


def assert_mode(chairman, expected, given):
    actual = (chairman.enabled, chairman.allow_quorum)
    assert actual == expected, (
        f"given={given!r}: expected chairman mode {expected!r}, got {actual!r}"
    )
    assert all(type(value) is bool for value in actual), (
        f"given={given!r}: existing boolean normalization must produce bools; got {actual!r}"
    )


def assert_actionable_rejection(call, given):
    try:
        call()
    except ValidationError as error:
        # Check semantics of the invariant and both migrations, without requiring
        # one exact sentence, a Pydantic version's wrapper, or error location.
        message = str(error).lower()
        compact = re.sub(r"[\s`\"']+", "", message)
        for key in ("chairman.allow_quorum", "chairman.enabled"):
            assert key in message, (
                f"given={given!r}: validation error must identify {key}; got {message!r}"
            )
        assert re.search(
            r"requir\w*|must\b|only\b|depend\w*|cannot\b|can't\b|incompatible\b", message
        ), f"given={given!r}: error must explain the dependency; got {message!r}"
        assert "enabled=true" in compact or "enabled:true" in compact, (
            f"given={given!r}: error must offer enabling chairman as a remedy; got {message!r}"
        )
        assert "allow_quorum=false" in compact or "allow_quorum:false" in compact, (
            f"given={given!r}: error must offer disabling quorum as a remedy; got {message!r}"
        )
    except Exception as error:
        pytest.fail(
            f"given={given!r}: expected pydantic.ValidationError, "
            f"got {type(error).__name__}: {error}"
        )
    else:
        pytest.fail(
            f"given={given!r}: disabled chairman with quorum must be rejected during validation"
        )


def write_yaml(tmp_path, filename, document):
    path = tmp_path / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def loaded_settings(loader, path, council_id=None):
    loaded = loader(str(path))
    if loader is load_council_catalog:
        return loaded.get(council_id or loaded.default_council_id)
    return loaded


@pytest.mark.parametrize("build", MODEL_ENTRY_POINTS)
@pytest.mark.parametrize("given,expected", VALID_INPUTS)
def test_valid_modes_and_existing_boolean_coercion_remain_accepted(build, given, expected):
    assert_mode(build(deepcopy(given)), expected, given)


@pytest.mark.parametrize("build", MODEL_ENTRY_POINTS)
@pytest.mark.parametrize("given", INVALID_INPUTS)
def test_invalid_mode_is_rejected_after_boolean_coercion_with_both_remedies(build, given):
    assert_actionable_rejection(lambda: build(deepcopy(given)), given)


@pytest.mark.parametrize("build", MODEL_ENTRY_POINTS)
def test_empty_chairman_preserves_all_existing_field_defaults(build):
    given = {}
    actual = build(given).model_dump()
    expected = {
        "enabled": False,
        "backend": "",
        "system_prompt_override": "",
        "allow_quorum": False,
        "parallel_member_evaluation": True,
    }
    assert actual == expected, (
        f"given={given!r}: existing chairman defaults must remain {expected!r}; got {actual!r}"
    )


def test_omitted_nested_chairman_preserves_disabled_unanimous_defaults():
    given = {}
    assert_mode(CouncilSettings.model_validate(given).chairman, (False, False), given)
    assert_mode(CouncilSettings().chairman, (False, False), "CouncilSettings()")
    assert_mode(ChairmanConfig(), (False, False), "ChairmanConfig()")


@pytest.mark.parametrize("build", MODEL_ENTRY_POINTS)
@pytest.mark.parametrize("allow_quorum", [False, True])
@pytest.mark.parametrize("backend", [None, "", "unregistered-chairman-backend"])
def test_enabled_modes_preserve_existing_acceptance_without_a_registered_backend(
    build, allow_quorum, backend
):
    given = {"enabled": True, "allow_quorum": allow_quorum}
    if backend is not None:
        given["backend"] = backend
    chairman = build(deepcopy(given))
    assert_mode(chairman, (True, allow_quorum), given)
    assert chairman.backend == (backend or ""), (
        f"given={given!r}: backend acceptance/value must remain unchanged"
    )


@pytest.mark.parametrize("build", MODEL_ENTRY_POINTS)
def test_valid_chairman_keeps_parallel_setting_and_prompt(build):
    given = {
        "enabled": True,
        "allow_quorum": False,
        "parallel_member_evaluation": False,
        "system_prompt_override": "QA fixture prompt",
    }
    chairman = build(deepcopy(given))
    assert chairman.parallel_member_evaluation is False, (
        f"given={given!r}: parallel setting was changed"
    )
    assert chairman.system_prompt_override == given["system_prompt_override"], (
        f"given={given!r}: prompt was changed"
    )


@pytest.mark.parametrize("build", MODEL_ENTRY_POINTS)
@pytest.mark.parametrize("given,expected", VALID_INPUTS)
def test_successful_validation_does_not_mutate_the_supplied_mapping(build, given, expected):
    supplied = deepcopy(given)
    before = deepcopy(supplied)
    assert_mode(build(supplied), expected, given)
    assert supplied == before, (
        f"given={before!r}: validation mutated supplied mapping to {supplied!r}"
    )


@pytest.mark.parametrize("build", MODEL_ENTRY_POINTS)
@pytest.mark.parametrize("given", INVALID_INPUTS)
def test_repeated_rejection_does_not_repair_or_mutate_the_supplied_mapping(build, given):
    supplied = deepcopy(given)
    before = deepcopy(supplied)
    for attempt in range(2):
        assert_actionable_rejection(lambda: build(supplied), {"input": before, "attempt": attempt})
        assert supplied == before, (
            f"given={before!r}, attempt={attempt}: rejection mutated input to {supplied!r}"
        )


def test_existing_council_extra_field_acceptance_is_preserved():
    given = {"some_future_key": 1, "chairman": {"enabled": True, "allow_quorum": False}}
    assert_mode(CouncilSettings.model_validate(given).chairman, (True, False), given)


@pytest.mark.parametrize("loader", LOADERS)
@pytest.mark.parametrize("given,expected", VALID_INPUTS)
def test_single_yaml_preserves_valid_modes_and_defaults(loader, tmp_path, given, expected):
    document = {"council_id": "qa-single", "chairman": given}
    path = write_yaml(tmp_path, "single-qa.yaml", document)
    assert_mode(loaded_settings(loader, path).chairman, expected, document)


@pytest.mark.parametrize("loader", LOADERS)
def test_single_yaml_without_chairman_preserves_defaults(loader, tmp_path):
    document = {"council_id": "qa-default"}
    path = write_yaml(tmp_path, "default-qa.yaml", document)
    assert_mode(loaded_settings(loader, path).chairman, (False, False), document)


@pytest.mark.parametrize("loader", LOADERS)
@pytest.mark.parametrize("given", INVALID_INPUTS)
def test_single_yaml_rejects_invalid_chairman_before_service_initialization(
    loader, tmp_path, given
):
    document = {"council_id": "qa-invalid", "chairman": given}
    path = write_yaml(tmp_path, "invalid-qa.yaml", document)
    before = path.read_bytes()
    assert_actionable_rejection(lambda: loader(str(path)), document)
    assert path.read_bytes() == before, f"given={document!r}: rejection must not rewrite the YAML"


def catalog_document(override):
    return {
        "default_council_id": "sample",
        "shared": {"chairman": {"enabled": True, "allow_quorum": True}},
        "councils": {"sample": {"chairman": override}},
    }


@pytest.mark.parametrize("loader", LOADERS)
def test_shared_catalog_override_cannot_disable_a_quorum_chairman(loader, tmp_path):
    document = catalog_document({"enabled": False})
    path = write_yaml(tmp_path, "catalog-qa.yaml", document)
    assert_actionable_rejection(lambda: loader(str(path)), document)


@pytest.mark.parametrize("loader", LOADERS)
@pytest.mark.parametrize(
    "override,expected",
    [
        pytest.param({"enabled": True}, (True, True), id="enable-chairman"),
        pytest.param({"allow_quorum": False}, (True, False), id="disable-quorum"),
        pytest.param({"enabled": False, "allow_quorum": False}, (False, False), id="disable-both"),
    ],
)
def test_shared_catalog_accepts_both_remedies_and_all_valid_resolved_modes(
    loader, tmp_path, override, expected
):
    document = catalog_document(override)
    path = write_yaml(tmp_path, "catalog-qa.yaml", document)
    assert_mode(loaded_settings(loader, path).chairman, expected, document)


@pytest.mark.parametrize("loader", LOADERS)
@pytest.mark.parametrize("invalid_first", [False, True])
def test_invalid_nondefault_council_rejects_the_entire_catalog_regardless_of_order(
    loader, tmp_path, invalid_first
):
    entries = [
        ("good", {"chairman": {"enabled": True, "allow_quorum": False}}),
        ("bad", {"chairman": {"enabled": False, "allow_quorum": True}}),
    ]
    if invalid_first:
        entries.reverse()
    document = {"default_council_id": "good", "councils": dict(entries)}
    path = write_yaml(tmp_path, "nondefault-qa.yaml", document)
    assert_actionable_rejection(lambda: loader(str(path)), document)


@pytest.mark.parametrize("loader", LOADERS)
@pytest.mark.parametrize(
    "override,expected",
    [
        pytest.param({"enabled": False}, None, id="inherited-quorum-now-invalid"),
        pytest.param(
            {"enabled": False, "allow_quorum": False},
            (False, False),
            id="explicitly-disabled-unanimous",
        ),
        pytest.param({"enabled": True}, (True, True), id="enabled-quorum"),
        pytest.param({"allow_quorum": False}, (True, False), id="enabled-unanimous"),
    ],
)
def test_referenced_profile_uses_final_deep_merged_chairman(
    loader, tmp_path, monkeypatch, override, expected
):
    profile = {"council_id": "sample", "chairman": {"enabled": True, "allow_quorum": True}}
    profile_path = write_yaml(tmp_path, "profiles/profile-qa.yaml", profile)
    document = {
        "default_council_id": "sample",
        "councils": {"sample": {"config_file": "profiles/profile-qa.yaml", "chairman": override}},
    }
    path = write_yaml(tmp_path, "catalog-qa.yaml", document)
    before = (path.read_bytes(), profile_path.read_bytes())
    # Resolve references relative to the catalog, independent of process cwd.
    elsewhere = tmp_path / "unrelated-cwd-qa"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    given = {"catalog": document, "profile": profile}
    if expected is None:
        assert_actionable_rejection(lambda: loader(str(path)), given)
    else:
        assert_mode(loaded_settings(loader, path).chairman, expected, given)
    assert (path.read_bytes(), profile_path.read_bytes()) == before, (
        f"given={given!r}: loader rewrote input files"
    )


@pytest.mark.parametrize(
    "relative_path",
    [
        "configs/council.yaml",
        "deploy/kubernetes/nomos/files/council/council.yaml",
    ],
)
def test_shipped_yaml_preserves_enabled_unanimous_mode_and_all_parsed_values(relative_path):
    # Pin the complete shipped profile, including the intentional Nemotron 3.5
    # default migration. Comments/formatting are intentionally ignored; the
    # public-config tests separately assert the model and every role's routing.
    root = Path(settings_module.__file__).resolve().parents[2]
    document = yaml.safe_load((root / relative_path).read_text(encoding="utf-8"))
    mode = document["chairman"]
    assert mode["enabled"] is True and mode["allow_quorum"] is False, (
        f"given={relative_path!r}: shipped mode must remain "
        f"enabled=true/allow_quorum=false; got {mode!r}"
    )
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    # Public pinned YAML semantic SHA-256, not a credential; retain the full literal.
    expected = "062de80d1a1b58b8adf404763439af9b5e5fe4a65784e0bb1afa9a0562c69e34"  # pragma: allowlist secret  # noqa: E501
    assert digest == expected, (
        f"given={relative_path!r}: all parsed release-template values must match pinned base; "
        f"expected semantic SHA-256 {expected}, got {digest}"
    )
