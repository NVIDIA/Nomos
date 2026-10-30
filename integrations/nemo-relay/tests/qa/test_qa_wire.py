# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""DESIGN.md `wire`: canonical, sha256_hex, bounded."""

from __future__ import annotations

import hashlib
import math

import pytest

from nemo_relay_nomos import wire


def test_canonical_sorts_keys_and_uses_compact_separators():
    value = {"b": 1, "a": [1, {"z": None, "y": True}], "c": "x"}
    got = wire.canonical(value)
    assert got == b'{"a":[1,{"y":true,"z":null}],"b":1,"c":"x"}', f"canonical({value!r}) -> {got!r}"


def test_canonical_returns_bytes():
    got = wire.canonical({"k": "v"})
    assert isinstance(got, bytes), f"canonical({{'k':'v'}}) returned {type(got)!r}, not bytes"


def test_canonical_of_empty_containers():
    assert wire.canonical({}) == b"{}", "canonical({}) must be b'{}'"
    assert wire.canonical([]) == b"[]", "canonical([]) must be b'[]'"


@pytest.mark.parametrize(
    "value",
    [math.nan, math.inf, -math.inf, {"a": math.nan}, [1, math.inf], {"n": {"m": -math.inf}}],
    ids=["nan", "inf", "-inf", "nested-nan", "list-inf", "deep-neg-inf"],
)
def test_canonical_raises_value_error_on_nan_or_infinity(value):
    with pytest.raises(ValueError):
        wire.canonical(value)
    # (message carries the input via pytest id)


def test_canonical_output_decodes_as_utf8():
    value = {"name": "café ✓"}
    got = wire.canonical(value)
    assert got.decode("utf-8"), f"canonical({value!r}) is not valid UTF-8: {got!r}"


def test_sha256_hex_encodes_a_string_as_utf8_first():
    text = "héllo ✓"
    got = wire.sha256_hex(text)
    want = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert got == want, f"sha256_hex({text!r}) -> {got!r}, want {want!r}"


def test_sha256_hex_of_bytes_is_plain_sha256():
    data = b"\x00\x01binary"
    got = wire.sha256_hex(data)
    assert got == hashlib.sha256(data).hexdigest(), f"sha256_hex({data!r}) -> {got!r}"


def test_sha256_hex_of_empty_string_equals_empty_bytes():
    assert wire.sha256_hex("") == wire.sha256_hex(b"") == hashlib.sha256(b"").hexdigest(), (
        "sha256_hex('') must equal sha256 of empty bytes"
    )


@pytest.mark.parametrize("text,limit", [("", 0), ("", 5), ("abc", 3), ("abc", 4), ("x" * 100, 100)])
def test_bounded_returns_text_unchanged_when_within_limit(text, limit):
    got = wire.bounded(text, limit)
    assert got == text, f"bounded({text!r}, {limit}) -> {got!r}; must be unchanged when len <= limit"


@pytest.mark.parametrize("text,limit", [("abcd", 3), ("x" * 101, 100), ("hello world", 0), ("é✓é✓", 2)])
def test_bounded_truncates_to_limit_and_appends_marker_with_count(text, limit):
    got = wire.bounded(text, limit)
    want = text[:limit] + f"…[truncated {len(text) - limit} chars]"
    assert got == want, f"bounded({text!r}, {limit}) -> {got!r}, want {want!r}"
