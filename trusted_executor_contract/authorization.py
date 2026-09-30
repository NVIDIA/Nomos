# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Strict, execution-independent verification of Council authorization writs."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn

from pydantic import ValidationError

from .models import WritOfExecution

DEFAULT_MAX_WRIT_BYTES = 64 * 1024
MAX_TRUSTED_KEYS = 8

_WRIT_FIELDS = frozenset(WritOfExecution.model_fields)
_PACKAGED_FILE_FIELDS = frozenset({"path", "content_base64", "sha256", "mode", "description"})
_VOTE_FIELDS = frozenset({"member_id", "decision", "risk_concerns"})


class AuthorizationErrorCode(StrEnum):
    """Stable, non-secret-bearing verification failure categories."""

    MALFORMED = "malformed"
    TOO_LARGE = "too_large"
    INVALID_SIGNATURE = "invalid_signature"
    UNTRUSTED_KEY = "untrusted_key"
    INVALID_TIME = "invalid_time"
    EXPIRED = "expired"
    IDENTITY_MISMATCH = "identity_mismatch"
    OPERATION_MISMATCH = "operation_mismatch"
    UNSUPPORTED = "unsupported"


class AuthorizationVerificationError(ValueError):
    """Raised when a writ cannot authorize the expected operation.

    The exception deliberately carries only a stable code and safe message. It
    never includes the raw writ, command, signature, nonce, or key material.
    """

    def __init__(self, code: AuthorizationErrorCode, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AuthorizationExpectation:
    """Trusted values the caller requires the signed writ to bind."""

    council_id: str
    agent_id: str
    command: str
    working_directory: str
    max_lifetime_seconds: float = 300.0
    clock_skew_seconds: float = 5.0
    allow_packaged_files: bool = False


@dataclass(frozen=True)
class VerifiedAuthorization:
    """A verified writ and the fingerprint of the pinned key that signed it."""

    writ: WritOfExecution
    key_fingerprint: str


def _fail(code: AuthorizationErrorCode, message: str) -> NoReturn:
    raise AuthorizationVerificationError(code, message)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            _fail(AuthorizationErrorCode.MALFORMED, "writ JSON contains duplicate fields")
        value[key] = item
    return value


def _reject_non_finite(_: str) -> NoReturn:
    _fail(AuthorizationErrorCode.MALFORMED, "writ JSON contains a non-finite number")


def _check_exact_fields(value: dict[str, Any], allowed: frozenset[str], label: str) -> None:
    extras = set(value).difference(allowed)
    if extras:
        _fail(AuthorizationErrorCode.MALFORMED, f"{label} contains unknown fields")


def _decode_writ(blob: str, max_writ_bytes: int) -> WritOfExecution:
    if not isinstance(blob, str) or not blob:
        _fail(AuthorizationErrorCode.MALFORMED, "writ must be non-empty base64 text")
    if max_writ_bytes <= 0:
        raise ValueError("max_writ_bytes must be positive")
    # A base64 value is at most 4/3 the decoded length, plus padding.
    if len(blob) > ((max_writ_bytes + 2) // 3) * 4:
        _fail(AuthorizationErrorCode.TOO_LARGE, "writ exceeds the configured size limit")
    try:
        encoded = blob.encode("ascii")
        payload = base64.b64decode(encoded, validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise AuthorizationVerificationError(
            AuthorizationErrorCode.MALFORMED, "writ is not valid base64"
        ) from exc
    if len(payload) > max_writ_bytes:
        _fail(AuthorizationErrorCode.TOO_LARGE, "writ exceeds the configured size limit")
    try:
        decoded = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except AuthorizationVerificationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthorizationVerificationError(
            AuthorizationErrorCode.MALFORMED, "writ is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(decoded, dict):
        _fail(AuthorizationErrorCode.MALFORMED, "writ JSON must be an object")
    _check_exact_fields(decoded, _WRIT_FIELDS, "writ")
    packaged_files = decoded.get("packaged_files", [])
    votes = decoded.get("member_votes", [])
    if not isinstance(packaged_files, list) or not isinstance(votes, list):
        _fail(AuthorizationErrorCode.MALFORMED, "writ list fields have invalid types")
    for item in packaged_files:
        if not isinstance(item, dict):
            _fail(AuthorizationErrorCode.MALFORMED, "packaged file must be an object")
        _check_exact_fields(item, _PACKAGED_FILE_FIELDS, "packaged file")
    for item in votes:
        if not isinstance(item, dict):
            _fail(AuthorizationErrorCode.MALFORMED, "member vote must be an object")
        _check_exact_fields(item, _VOTE_FIELDS, "member vote")
    try:
        return WritOfExecution.model_validate(decoded)
    except ValidationError as exc:
        raise AuthorizationVerificationError(
            AuthorizationErrorCode.MALFORMED, "writ does not match the authorization schema"
        ) from exc


def _validate_signed_shape(writ: WritOfExecution) -> None:
    if not writ.writ_id or not writ.council_id or not writ.target_agent_id:
        _fail(AuthorizationErrorCode.MALFORMED, "writ identity fields must be non-empty")
    hex_fields = ((writ.intent_hash, 64), (writ.nonce, 64), (writ.signature, 128))
    for value, expected_length in hex_fields:
        if len(value) != expected_length:
            _fail(AuthorizationErrorCode.MALFORMED, "writ cryptographic field has invalid length")
        try:
            bytes.fromhex(value)
        except ValueError as exc:
            raise AuthorizationVerificationError(
                AuthorizationErrorCode.MALFORMED,
                "writ cryptographic field is not hexadecimal",
            ) from exc
    if not all(math.isfinite(value) for value in (writ.issued_at, writ.expires_at)):
        _fail(AuthorizationErrorCode.MALFORMED, "writ timestamps must be finite")


def verify_authorization_writ(
    writ_blob: str,
    *,
    council_public_keys: Sequence[bytes],
    expectation: AuthorizationExpectation,
    now: float | None = None,
    max_writ_bytes: int = DEFAULT_MAX_WRIT_BYTES,
) -> VerifiedAuthorization:
    """Verify that a signed writ authorizes one exact native operation.

    Public keys must come from trusted deployment configuration, never from the
    authorization response being verified. Nonce consumption is intentionally
    separate because it is stateful and must be atomic across processes.
    """

    keys = tuple(council_public_keys)
    if not keys or len(keys) > MAX_TRUSTED_KEYS:
        _fail(AuthorizationErrorCode.UNTRUSTED_KEY, "trusted Council key set is invalid")
    if any(not isinstance(key, bytes) or len(key) != 32 for key in keys):
        _fail(AuthorizationErrorCode.UNTRUSTED_KEY, "trusted Council key is invalid")
    if (
        not math.isfinite(expectation.max_lifetime_seconds)
        or not math.isfinite(expectation.clock_skew_seconds)
        or expectation.max_lifetime_seconds <= 0
        or expectation.clock_skew_seconds < 0
    ):
        raise ValueError("authorization time bounds are invalid")

    writ = _decode_writ(writ_blob, max_writ_bytes)
    _validate_signed_shape(writ)

    matched_key: bytes | None = None
    for key in keys:
        if writ.verify(key):
            matched_key = key
            break
    if matched_key is None:
        _fail(AuthorizationErrorCode.INVALID_SIGNATURE, "writ signature verification failed")

    timestamp = time.time() if now is None else now
    if not math.isfinite(timestamp):
        raise ValueError("now must be finite")
    if writ.expires_at <= writ.issued_at:
        _fail(AuthorizationErrorCode.INVALID_TIME, "writ validity interval is invalid")
    if writ.expires_at - writ.issued_at > expectation.max_lifetime_seconds:
        _fail(AuthorizationErrorCode.INVALID_TIME, "writ validity interval is too long")
    if timestamp + expectation.clock_skew_seconds < writ.issued_at:
        _fail(AuthorizationErrorCode.INVALID_TIME, "writ is not yet valid")
    if timestamp - expectation.clock_skew_seconds > writ.expires_at:
        _fail(AuthorizationErrorCode.EXPIRED, "writ has expired")
    if writ.max_executions != 1:
        _fail(AuthorizationErrorCode.UNSUPPORTED, "writ is not single-use")
    if writ.council_id != expectation.council_id or writ.target_agent_id != expectation.agent_id:
        _fail(AuthorizationErrorCode.IDENTITY_MISMATCH, "writ identity binding does not match")
    if (
        writ.command != expectation.command
        or writ.working_directory != expectation.working_directory
    ):
        _fail(AuthorizationErrorCode.OPERATION_MISMATCH, "writ operation binding does not match")
    if writ.packaged_files and not expectation.allow_packaged_files:
        _fail(
            AuthorizationErrorCode.UNSUPPORTED,
            "packaged files are not supported for native execution",
        )

    return VerifiedAuthorization(
        writ=writ,
        key_fingerprint=hashlib.sha256(matched_key).hexdigest(),
    )
