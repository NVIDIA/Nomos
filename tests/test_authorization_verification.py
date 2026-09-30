# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Strict verification tests for native-execution authorization writs."""

import base64
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from trusted_executor_contract import (
    AuthorizationErrorCode,
    AuthorizationExpectation,
    AuthorizationVerificationError,
    PackagedFile,
    WritOfExecution,
    verify_authorization_writ,
)


def _signed_writ(**overrides):
    private_key = Ed25519PrivateKey.generate()
    values = {
        "writ_id": "writ-1",
        "intent_hash": "ab" * 32,
        "command": "echo approved",
        "working_directory": "/workspace",
        "target_agent_id": "agent-1",
        "issued_at": 1_000.0,
        "expires_at": 1_060.0,
        "max_executions": 1,
        "nonce": "cd" * 32,
        "council_id": "council-1",
    }
    values.update(overrides)
    writ = WritOfExecution(**values)
    writ.signature = private_key.sign(writ.canonical_bytes()).hex()
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    blob = base64.b64encode(writ.model_dump_json().encode()).decode()
    return writ, blob, public_key


def _expectation(**overrides):
    values = {
        "council_id": "council-1",
        "agent_id": "agent-1",
        "command": "echo approved",
        "working_directory": "/workspace",
    }
    values.update(overrides)
    return AuthorizationExpectation(**values)


def test_verifies_exact_operation_and_reports_pinned_key_fingerprint():
    writ, blob, public_key = _signed_writ()

    verified = verify_authorization_writ(
        blob,
        council_public_keys=[public_key],
        expectation=_expectation(),
        now=1_010.0,
    )

    assert verified.writ == writ
    assert len(verified.key_fingerprint) == 64


@pytest.mark.parametrize(
    ("expectation", "code"),
    [
        (_expectation(command="echo changed"), AuthorizationErrorCode.OPERATION_MISMATCH),
        (_expectation(working_directory="/other"), AuthorizationErrorCode.OPERATION_MISMATCH),
        (_expectation(agent_id="agent-2"), AuthorizationErrorCode.IDENTITY_MISMATCH),
        (_expectation(council_id="council-2"), AuthorizationErrorCode.IDENTITY_MISMATCH),
    ],
)
def test_rejects_binding_mismatch(expectation, code):
    _, blob, public_key = _signed_writ()
    with pytest.raises(AuthorizationVerificationError) as caught:
        verify_authorization_writ(
            blob,
            council_public_keys=[public_key],
            expectation=expectation,
            now=1_010.0,
        )
    assert caught.value.code == code


@pytest.mark.parametrize(
    ("overrides", "now", "code"),
    [
        ({"max_executions": 2}, 1_010.0, AuthorizationErrorCode.UNSUPPORTED),
        ({"issued_at": 1_020.0}, 1_010.0, AuthorizationErrorCode.INVALID_TIME),
        ({}, 1_070.0, AuthorizationErrorCode.EXPIRED),
        ({"expires_at": 1_500.0}, 1_010.0, AuthorizationErrorCode.INVALID_TIME),
        (
            {"packaged_files": [PackagedFile(path="script.sh", content_base64="ZWNobyBoaQ==")]},
            1_010.0,
            AuthorizationErrorCode.UNSUPPORTED,
        ),
    ],
)
def test_rejects_unsafe_signed_semantics(overrides, now, code):
    _, blob, public_key = _signed_writ(**overrides)
    with pytest.raises(AuthorizationVerificationError) as caught:
        verify_authorization_writ(
            blob,
            council_public_keys=[public_key],
            expectation=_expectation(),
            now=now,
        )
    assert caught.value.code == code


def test_rejects_tampering_and_unknown_or_duplicate_fields():
    _, blob, public_key = _signed_writ()
    payload = json.loads(base64.b64decode(blob))
    payload["command"] = "echo tampered"
    tampered = base64.b64encode(json.dumps(payload).encode()).decode()
    with pytest.raises(AuthorizationVerificationError) as caught:
        verify_authorization_writ(
            tampered,
            council_public_keys=[public_key],
            expectation=_expectation(),
            now=1_010.0,
        )
    assert caught.value.code == AuthorizationErrorCode.INVALID_SIGNATURE

    payload["unknown"] = True
    unknown = base64.b64encode(json.dumps(payload).encode()).decode()
    with pytest.raises(AuthorizationVerificationError) as caught:
        verify_authorization_writ(
            unknown,
            council_public_keys=[public_key],
            expectation=_expectation(),
            now=1_010.0,
        )
    assert caught.value.code == AuthorizationErrorCode.MALFORMED

    raw = base64.b64decode(blob).decode()
    duplicate = raw.replace('"writ_id":"writ-1"', '"writ_id":"writ-1","writ_id":"other"')
    duplicate_blob = base64.b64encode(duplicate.encode()).decode()
    with pytest.raises(AuthorizationVerificationError) as caught:
        verify_authorization_writ(
            duplicate_blob,
            council_public_keys=[public_key],
            expectation=_expectation(),
            now=1_010.0,
        )
    assert caught.value.code == AuthorizationErrorCode.MALFORMED


def test_rejects_untrusted_key_and_oversized_blob_without_leaking_payload():
    _, blob, _ = _signed_writ()
    other_key = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    )
    with pytest.raises(AuthorizationVerificationError) as caught:
        verify_authorization_writ(
            blob,
            council_public_keys=[other_key],
            expectation=_expectation(),
            now=1_010.0,
        )
    assert caught.value.code == AuthorizationErrorCode.INVALID_SIGNATURE
    assert "echo approved" not in str(caught.value)

    with pytest.raises(AuthorizationVerificationError) as caught:
        verify_authorization_writ(
            "A" * 1000,
            council_public_keys=[other_key],
            expectation=_expectation(),
            max_writ_bytes=16,
        )
    assert caught.value.code == AuthorizationErrorCode.TOO_LARGE


@pytest.mark.parametrize(
    "expectation",
    [
        _expectation(max_lifetime_seconds=float("nan")),
        _expectation(max_lifetime_seconds=float("inf")),
        _expectation(clock_skew_seconds=float("nan")),
    ],
)
def test_rejects_non_finite_verifier_bounds(expectation):
    _, blob, public_key = _signed_writ()
    with pytest.raises(ValueError, match="time bounds"):
        verify_authorization_writ(
            blob,
            council_public_keys=[public_key],
            expectation=expectation,
            now=1_010.0,
        )
