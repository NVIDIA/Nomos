# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""DESIGN.md `signing`: the plugin's Ed25519 identity."""

from __future__ import annotations

import copy
import hashlib

import pytest
import qa_support as qa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from nemo_relay_nomos.signing import PluginSigner, generate_seed, verify_plugin_signature

CONTENT = hashlib.sha256(b"qa content").hexdigest()


def test_identity_names_the_plugin_and_its_key():
    ident = PluginSigner(qa.PLUGIN_SEED).identity
    want = {
        "type": "plugin",
        "id": "nomos.council",
        "public_key_hex": qa.public_hex(qa.PLUGIN_SEED),
        "key_fingerprint": qa.fingerprint(qa.PLUGIN_SEED),
    }
    assert ident == want, f"identity for seed {qa.PLUGIN_SEED.hex()} is {ident!r}, want {want!r}"


def test_identity_is_deterministic_for_a_seed():
    assert PluginSigner(qa.PLUGIN_SEED).identity == PluginSigner(bytes(qa.PLUGIN_SEED)).identity


def test_sign_returns_the_specified_shape():
    sig = PluginSigner(qa.PLUGIN_SEED).sign(CONTENT, evidence_id="ev_" + "a" * 32, created_at=123.5)
    assert set(sig) == {"algorithm", "signed", "value"}, f"signature keys {sorted(sig)!r}"
    assert sig["algorithm"] == "ed25519", f"algorithm {sig['algorithm']!r}"
    assert sig["signed"] == {
        "evidence_id": "ev_" + "a" * 32,
        "content_sha256": CONTENT,
        "created_at": 123.5,
    }, f"signed block {sig['signed']!r}"
    bytes.fromhex(sig["value"])


def test_sign_value_is_ed25519_over_canonical_signed_block():
    sig = PluginSigner(qa.PLUGIN_SEED).sign(CONTENT, evidence_id="ev_" + "b" * 32, created_at=1.25)
    pub = Ed25519PublicKey.from_public_bytes(qa.public_bytes(qa.PLUGIN_SEED))
    pub.verify(bytes.fromhex(sig["value"]), qa.canonical(sig["signed"]))


def test_verify_plugin_signature_accepts_a_genuine_signature():
    sig = PluginSigner(qa.PLUGIN_SEED).sign(CONTENT, evidence_id="ev_" + "c" * 32, created_at=9.0)
    assert verify_plugin_signature(qa.public_hex(qa.PLUGIN_SEED), sig) is True, f"genuine {sig!r} rejected"


@pytest.mark.parametrize(
    "tamper",
    [
        lambda s: s["signed"].__setitem__("content_sha256", "0" * 64),
        lambda s: s["signed"].__setitem__("evidence_id", "ev_" + "f" * 32),
        lambda s: s["signed"].__setitem__("created_at", 10.0),
        lambda s: s.__setitem__("value", ("00" if s["value"][:2] != "00" else "11") + s["value"][2:]),
    ],
    ids=["content", "evidence_id", "created_at", "value"],
)
def test_verify_plugin_signature_rejects_tampering(tamper):
    sig = PluginSigner(qa.PLUGIN_SEED).sign(CONTENT, evidence_id="ev_" + "d" * 32, created_at=9.0)
    bad = copy.deepcopy(sig)
    tamper(bad)
    assert verify_plugin_signature(qa.public_hex(qa.PLUGIN_SEED), bad) is False, f"tampered {bad!r} accepted"


def test_verify_plugin_signature_rejects_another_key():
    sig = PluginSigner(qa.PLUGIN_SEED).sign(CONTENT, evidence_id="ev_" + "e" * 32, created_at=9.0)
    assert verify_plugin_signature(qa.public_hex(qa.STRANGER_SEED), sig) is False, (
        "signature verified under wrong key"
    )


@pytest.mark.parametrize(
    "key,sig",
    [
        ("not-hex", {"algorithm": "ed25519", "signed": {}, "value": "00"}),
        ("", None),
        (None, {}),
        ("ab" * 32, "string"),
        ("ab" * 32, {"algorithm": "ed25519"}),
        ("ab" * 31, {"algorithm": "ed25519", "signed": {"content_sha256": "x"}, "value": "zz"}),
        (123, []),
        ("ab" * 32, {"algorithm": "ed25519", "signed": {"x": float("nan")}, "value": "00" * 64}),
    ],
)
def test_verify_plugin_signature_never_raises(key, sig):
    try:
        got = verify_plugin_signature(key, sig)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"verify_plugin_signature({key!r}, {sig!r}) raised {exc!r}")
    assert got is False, f"verify_plugin_signature({key!r}, {sig!r}) -> {got!r}"


def test_generate_seed_returns_32_fresh_bytes():
    a, b = generate_seed(), generate_seed()
    assert isinstance(a, bytes) and len(a) == 32, f"generate_seed() -> {a!r}"
    assert a != b, "two generate_seed() calls returned the same seed"
    PluginSigner(a)
