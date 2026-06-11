# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Integrity envelope helpers for Redis-backed agent accounts."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping

ACCOUNT_INTEGRITY_FIELD = "_integrity_hmac"


def sign_account_record(
    *,
    key: str,
    agent_id: str,
    account: Mapping[str, str],
    permissions: set[str],
    councils: set[str],
) -> str:
    """Return an HMAC for account-critical Redis state."""
    return hmac.new(
        key.encode("utf-8"),
        _account_payload(
            agent_id=agent_id,
            account=account,
            permissions=permissions,
            councils=councils,
        ),
        hashlib.sha256,
    ).hexdigest()


def verify_account_record(
    *,
    key: str,
    agent_id: str,
    account: Mapping[str, str],
    permissions: set[str],
    councils: set[str],
) -> bool:
    """Return whether the stored account HMAC matches account-critical state."""
    expected = sign_account_record(
        key=key,
        agent_id=agent_id,
        account=account,
        permissions=permissions,
        councils=councils,
    )
    return hmac.compare_digest(account.get(ACCOUNT_INTEGRITY_FIELD, ""), expected)


def _account_payload(
    *,
    agent_id: str,
    account: Mapping[str, str],
    permissions: set[str],
    councils: set[str],
) -> bytes:
    signed_account = {
        key: value
        for key, value in account.items()
        if key not in {ACCOUNT_INTEGRITY_FIELD, "auth_token"}
    }
    payload = {
        "agent_id": agent_id,
        "account": dict(sorted(signed_account.items())),
        "permissions": sorted(permissions),
        "councils": sorted(councils),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
