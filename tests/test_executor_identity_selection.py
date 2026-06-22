# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


"""NOMOS-3: executor evidence identity selects Vault transit vs local file keys.

These tests run with ``nomos`` un-importable in the Vault path to mirror the
executor container (which ships ``nomos_runtime`` only) — the selector and its
Vault signer must be self-contained.
"""

from __future__ import annotations

import base64
import builtins
import io
import json

from executors.openshell.identity import build_executor_identity


class _FakeSigningConfig:
    def __init__(self, provider: str):
        self.provider = provider
        self.vault_addr = "http://vault:8200"
        self.vault_transit_mount = "transit"
        self.vault_namespace = ""
        # AppRole auth → no projected-token file needed on disk for the test.
        self.vault_k8s_role = ""
        self.vault_k8s_token_path = "/var/run/secrets/nomos/vault-token"
        self.vault_role_id = "role-id"
        self.vault_secret_id = "secret-id"


def _fake_vault_urlopen(pubkey: bytes):
    def urlopen(request, timeout=0):
        path = request.full_url.split("/v1/", 1)[1]
        if path.startswith("auth/"):
            payload = {"auth": {"client_token": "t"}}
        elif path.startswith("transit/keys/"):
            payload = {
                "data": {
                    "latest_version": 1,
                    "keys": {"1": {"public_key": base64.b64encode(pubkey).decode()}},
                }
            }
        else:
            raise AssertionError(f"unexpected vault path: {path}")
        resp = io.BytesIO(json.dumps(payload).encode())
        resp.__enter__ = lambda *a: resp
        resp.__exit__ = lambda *a: False
        return resp

    return urlopen


def _block_nomos(monkeypatch):
    """Make any ``import nomos[...]`` raise — as it would in the executor image."""
    real_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == "nomos" or name.startswith("nomos."):
            raise ModuleNotFoundError(f"No module named '{name}' (executor image has no nomos)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)


def test_local_provider_uses_file_keys():
    identity = build_executor_identity(
        private_key_path="", public_key_path="", signing=_FakeSigningConfig("local")
    )
    assert hasattr(identity, "private_key")  # ExecutorIdentity, not provider-backed
    assert len(identity.key_fingerprint) == 64


def test_local_provider_from_env_default(monkeypatch):
    monkeypatch.delenv("NOMOS_SIGNER_PROVIDER", raising=False)
    identity = build_executor_identity()
    assert hasattr(identity, "private_key")


def test_vault_provider_builds_transit_signer_identity_without_nomos(monkeypatch):
    # Regression for the merged bug: the Vault path must NOT import nomos.
    monkeypatch.setenv("EXECUTOR_SIGNER_KEY_ID", "nomos-executor")
    monkeypatch.setattr("urllib.request.urlopen", _fake_vault_urlopen(b"\x11" * 32))
    _block_nomos(monkeypatch)

    identity = build_executor_identity(signing=_FakeSigningConfig("vault"))

    assert not hasattr(identity, "private_key")  # provider-backed (no private key bytes)
    assert identity.key_id == "nomos-executor"
    assert len(identity.key_fingerprint) == 64


def test_vault_provider_from_env_without_nomos(monkeypatch):
    monkeypatch.setenv("NOMOS_SIGNER_PROVIDER", "vault")
    monkeypatch.setenv("EXECUTOR_SIGNER_KEY_ID", "nomos-executor")
    monkeypatch.setenv("NOMOS_VAULT_ADDR", "http://vault:8200")
    monkeypatch.setenv("VAULT_ROLE_ID", "rid")
    monkeypatch.setenv("VAULT_SECRET_ID", "sid")
    monkeypatch.setattr("urllib.request.urlopen", _fake_vault_urlopen(b"\x22" * 32))
    _block_nomos(monkeypatch)

    identity = build_executor_identity()

    assert not hasattr(identity, "private_key")
    assert identity.key_id == "nomos-executor"
