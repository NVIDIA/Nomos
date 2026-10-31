# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""DESIGN.md `values`: configuration value references."""

from __future__ import annotations

import copy
import os
from pathlib import Path

import pytest

from nemo_relay_nomos import values
from nemo_relay_nomos.values import ConfigReferenceError

SECRET = "s3cr3t-QA-VALUE-do-not-leak-4e1b"


def _secret_file(
    tmp_path: Path, content: str = SECRET + "\n", mode: int = 0o600, name: str = "secret"
) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    os.chmod(path, mode)
    return path


# ---- ConfigReferenceError -------------------------------------------------------------------------


def test_config_reference_error_is_a_value_error_with_field_and_message():
    with pytest.raises(ConfigReferenceError) as info:
        values.resolve({"env": "QA_MISSING_VAR"}, field="connection.auth_token", environ={})
    err = info.value
    assert isinstance(err, ValueError), "ConfigReferenceError must subclass ValueError"
    assert err.field == "connection.auth_token", (
        f"field attr is {err.field!r} for field='connection.auth_token'"
    )
    assert isinstance(err.message, str) and err.message, (
        f"message attr must be a non-empty str, got {err.message!r}"
    )


# ---- is_reference ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        {"value": 1},
        {"value": None},
        {"env": "X"},
        {"file": "/x"},
        {"encrypted": "abc", "key_env": "K"},
        {"encrypted": "abc", "key_file": "/k"},
    ],
)
def test_is_reference_true_for_each_allowed_key_set(value):
    assert values.is_reference(value) is True, f"is_reference({value!r}) must be True"


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"env": "X", "value": 1},
        {"encrypted": "abc"},
        {"key_env": "K"},
        {"encrypted": "abc", "key_env": "K", "key_file": "/k"},
        {"file": "/x", "extra": 1},
        {"Value": 1},
        "env",
        ["env"],
        None,
        42,
        {"connection": {"env": "X"}},
    ],
)
def test_is_reference_false_for_anything_else(value):
    assert values.is_reference(value) is False, f"is_reference({value!r}) must be False"


# ---- resolve --------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["literal", 3, 2.5, True, None, [1, 2], {"a": 1}, {"env": "X", "value": 2}])
def test_resolve_returns_a_non_reference_unchanged(value):
    original = copy.deepcopy(value)
    got = values.resolve(value, field="f", environ={})
    assert got == original, f"resolve({value!r}) -> {got!r}; a non-reference must be unchanged"


@pytest.mark.parametrize("inner", ["text", 0, False, 1.5, [1], {"nested": "dict"}])
def test_resolve_value_reference_returns_the_inner_value(inner):
    got = values.resolve({"value": inner}, field="f", environ={})
    assert got == inner, f"resolve({{'value': {inner!r}}}) -> {got!r}"


def test_resolve_env_reference_reads_the_given_environ():
    got = values.resolve({"env": "QA_TOKEN"}, field="f", environ={"QA_TOKEN": SECRET})
    assert got == SECRET, f"resolve({{'env':'QA_TOKEN'}}, environ={{QA_TOKEN:...}}) -> {got!r}"


def test_resolve_env_reference_defaults_to_process_environment(monkeypatch):
    monkeypatch.setenv("QA_PROCESS_TOKEN", "from-process")
    got = values.resolve({"env": "QA_PROCESS_TOKEN"}, field="f")
    assert got == "from-process", f"resolve(env QA_PROCESS_TOKEN, environ=None) -> {got!r}"


@pytest.mark.parametrize("environ", [{}, {"QA_EMPTY": ""}], ids=["missing", "empty"])
def test_resolve_env_reference_missing_or_empty_raises_naming_the_variable(environ):
    with pytest.raises(ConfigReferenceError) as info:
        values.resolve({"env": "QA_EMPTY"}, field="connection.auth_token", environ=environ)
    msg = info.value.message
    assert "QA_EMPTY" in msg, f"message {msg!r} must name the variable QA_EMPTY (environ={environ!r})"


def test_resolve_env_error_says_managed_workers_start_with_minimal_environment():
    with pytest.raises(ConfigReferenceError) as info:
        values.resolve({"env": "QA_ABSENT"}, field="f", environ={})
    msg = info.value.message.lower()
    assert "minimal" in msg and "environment" in msg, (
        f"message {info.value.message!r} for missing QA_ABSENT must say managed Relay workers start with a "
        "minimal environment"
    )


@pytest.mark.parametrize(
    "content,want",
    [
        (SECRET + "\n", SECRET),
        (SECRET, SECRET),
        (SECRET + "\n\n", SECRET + "\n"),
        ("line1\nline2\n", "line1\nline2"),
        ("", ""),
    ],
)
def test_resolve_file_reference_strips_exactly_one_trailing_newline(tmp_path, content, want):
    path = _secret_file(tmp_path, content)
    got = values.resolve({"file": str(path)}, field="f", environ={})
    assert got == want, f"resolve(file containing {content!r}) -> {got!r}, want {want!r}"


@pytest.mark.parametrize("mode", [0o600, 0o400])
def test_resolve_file_reference_accepts_owner_only_modes(tmp_path, mode):
    path = _secret_file(tmp_path, mode=mode)
    got = values.resolve({"file": str(path)}, field="f", environ={})
    assert got == SECRET, f"file at mode {oct(mode)} must resolve, got {got!r}"


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660, 0o666, 0o610, 0o601])
def test_resolve_file_reference_rejects_group_or_other_permission_bits(tmp_path, mode):
    path = _secret_file(tmp_path, mode=mode)
    with pytest.raises(ConfigReferenceError) as info:
        values.resolve({"file": str(path)}, field="connection.auth_token", environ={})
    assert SECRET not in info.value.message and SECRET not in str(info.value), (
        f"error for mode {oct(mode)} leaked the file's secret: {info.value.message!r}"
    )


def test_resolve_file_reference_rejects_a_relative_path(tmp_path, monkeypatch):
    _secret_file(tmp_path, name="rel_secret")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigReferenceError):
        values.resolve({"file": "rel_secret"}, field="f", environ={})


def test_resolve_file_reference_rejects_a_symlink_even_to_a_valid_file(tmp_path):
    target = _secret_file(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ConfigReferenceError) as info:
        values.resolve({"file": str(link)}, field="f", environ={})
    assert SECRET not in str(info.value), f"symlink refusal leaked the secret: {info.value!s}"


def test_resolve_file_reference_rejects_a_directory(tmp_path):
    d = tmp_path / "dir"
    d.mkdir(mode=0o700)
    with pytest.raises(ConfigReferenceError):
        values.resolve({"file": str(d)}, field="f", environ={})


def test_resolve_file_reference_rejects_a_missing_file(tmp_path):
    with pytest.raises(ConfigReferenceError):
        values.resolve({"file": str(tmp_path / "nope")}, field="f", environ={})


def test_resolve_file_reference_accepts_exactly_64_kib(tmp_path):
    content = "a" * (64 * 1024)
    path = _secret_file(tmp_path, content)
    got = values.resolve({"file": str(path)}, field="f", environ={})
    assert got == content, "a 65536-byte file (exactly 64 KiB) must resolve"


def test_resolve_file_reference_rejects_one_byte_over_64_kib(tmp_path):
    content = SECRET + "a" * (64 * 1024 + 1 - len(SECRET))
    path = _secret_file(tmp_path, content)
    assert path.stat().st_size == 64 * 1024 + 1
    with pytest.raises(ConfigReferenceError) as info:
        values.resolve({"file": str(path)}, field="f", environ={})
    assert SECRET not in str(info.value), f"oversize refusal leaked file content: {str(info.value)[:200]!r}"


@pytest.mark.parametrize(
    "ref",
    [
        {"encrypted": "ciphertext", "key_env": "QA_KEY"},
        {"encrypted": "ciphertext", "key_file": "/nonexistent/key"},
    ],
    ids=["key_env", "key_file"],
)
def test_resolve_encrypted_reference_always_raises_not_supported_yet(ref):
    with pytest.raises(ConfigReferenceError) as info:
        values.resolve(ref, field="connection.auth_token", environ={"QA_KEY": SECRET})
    assert "not supported yet" in info.value.message, (
        f"encrypted ref {ref!r} message {info.value.message!r} must contain 'not supported yet'"
    )


def test_encrypted_reference_error_never_contains_the_key_value(tmp_path):
    with pytest.raises(ConfigReferenceError) as info:
        values.resolve({"encrypted": "ct", "key_env": "QA_KEY"}, field="f", environ={"QA_KEY": SECRET})
    assert SECRET not in str(info.value) and SECRET not in info.value.message, (
        f"encrypted/key_env error leaked the key: {info.value.message!r}"
    )
    key = _secret_file(tmp_path, name="key")
    with pytest.raises(ConfigReferenceError) as info:
        values.resolve({"encrypted": "ct", "key_file": str(key)}, field="f", environ={})
    assert SECRET not in str(info.value), f"encrypted/key_file error leaked the key: {info.value.message!r}"


# ---- resolve_tree ---------------------------------------------------------------------------------


def test_resolve_tree_resolves_references_anywhere_in_the_tree(tmp_path):
    path = _secret_file(tmp_path)
    tree = {
        "connection": {"auth_token": {"env": "QA_TOKEN"}, "proxy_token": {"file": str(path)}},
        "max_denials": {"value": 5},
        "list": [{"value": "a"}, "b"],
        "plain": "unchanged",
    }
    got = values.resolve_tree(tree, environ={"QA_TOKEN": "tok"})
    want = {
        "connection": {"auth_token": "tok", "proxy_token": SECRET},
        "max_denials": 5,
        "list": ["a", "b"],
        "plain": "unchanged",
    }
    assert got == want, f"resolve_tree({tree!r}) -> {got!r}, want {want!r}"


def test_resolve_tree_never_mutates_its_input():
    tree = {"connection": {"auth_token": {"env": "QA_TOKEN"}}, "xs": [{"value": 1}]}
    before = copy.deepcopy(tree)
    values.resolve_tree(tree, environ={"QA_TOKEN": "tok"})
    assert tree == before, f"resolve_tree mutated its input: {tree!r} != {before!r}"


def test_resolve_tree_error_field_is_the_dotted_path():
    tree = {"connection": {"auth_token": {"env": "QA_MISSING"}}}
    with pytest.raises(ConfigReferenceError) as info:
        values.resolve_tree(tree, environ={})
    assert info.value.field == "connection.auth_token", (
        f"field for {tree!r} is {info.value.field!r}, want 'connection.auth_token'"
    )


def test_resolve_tree_error_never_contains_an_already_resolved_secret():
    tree = {"a_first": {"env": "QA_GOOD"}, "z_second": {"env": "QA_MISSING"}}
    with pytest.raises(ConfigReferenceError) as info:
        values.resolve_tree(tree, environ={"QA_GOOD": SECRET})
    assert SECRET not in str(info.value) and SECRET not in info.value.message, (
        f"resolve_tree error leaked a resolved secret: {info.value.message!r}"
    )


def test_resolve_tree_of_a_tree_without_references_is_equal():
    tree = {"a": [1, "x", {"b": None}], "c": True}
    got = values.resolve_tree(copy.deepcopy(tree), environ={})
    assert got == tree, f"resolve_tree({tree!r}) -> {got!r}"
