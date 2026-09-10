# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the executor-side (nomos-free) Vault transit signer (NOMOS-3)."""

from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from executors.common.vault_signer import (
    ExecutorVaultSignerError,
    ExecutorVaultTransitSigner,
)
from nomos.level_0.crypto.signing import KeyPair


class _FakeVault:
    """Stands in for urllib.request.urlopen against a Vault transit engine."""

    def __init__(self):
        self.key_pair = KeyPair.generate(key_id="vault-key")
        self.requests: list[tuple[str, str, dict | None]] = []
        self.fail_first_with_403 = False
        self.login_count = 0

    def urlopen(self, request, timeout=0, context=None):
        import io
        import urllib.error

        body = json.loads(request.data) if request.data else None
        path = request.full_url.split("/v1/", 1)[1]
        self.requests.append((request.get_method(), path, body))

        if path.startswith("auth/"):
            self.login_count += 1
            payload = {"auth": {"client_token": f"token-{self.login_count}"}}
        elif self.fail_first_with_403:
            self.fail_first_with_403 = False
            raise urllib.error.HTTPError(request.full_url, 403, "forbidden", {}, None)
        elif path.startswith("transit/sign/"):
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

            data = base64.b64decode(body["input"])
            key = Ed25519PrivateKey.from_private_bytes(self.key_pair.private_key)
            sig = base64.b64encode(key.sign(data)).decode()
            payload = {"data": {"signature": f"vault:v1:{sig}"}}
        elif path.startswith("transit/keys/"):
            payload = {
                "data": {
                    "latest_version": 1,
                    "keys": {
                        "1": {"public_key": base64.b64encode(self.key_pair.public_key).decode()}
                    },
                }
            }
        else:
            raise AssertionError(f"unexpected vault path: {path}")

        response = io.BytesIO(json.dumps(payload).encode())
        response.__enter__ = lambda *a: response
        response.__exit__ = lambda *a: False
        return response


@pytest.fixture
def fake_vault(monkeypatch):
    vault = _FakeVault()
    monkeypatch.setattr("executors.common.vault_signer.urlopen_no_redirect", vault.urlopen)
    return vault


def _signer(**overrides) -> ExecutorVaultTransitSigner:
    kwargs = dict(
        key_id="nomos-executor",
        vault_addr="https://vault.example",
        role_id="role-id",
        secret_id="secret-id",
    )
    kwargs.update(overrides)
    return ExecutorVaultTransitSigner(**kwargs)


def _verify(public_key_bytes: bytes, payload: bytes, signature_bytes: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(public_key_bytes).verify(signature_bytes, payload)
        return True
    except Exception:
        return False


class _FakeSigning:
    vault_addr = "https://vault.example"
    vault_transit_mount = "transit"
    vault_namespace = ""
    vault_k8s_role = ""
    vault_k8s_token_path = "/var/run/secrets/nomos/vault-token"
    vault_role_id = "role-id"
    vault_secret_id = "secret-id"


def test_signs_via_transit_and_verifies(fake_vault):
    signer = _signer()
    sig = signer.sign(b"executor evidence bytes")
    assert sig.signer_id == "nomos-executor"
    assert _verify(signer.public_key_bytes(), b"executor evidence bytes", sig.signature_bytes)
    assert signer.fingerprint() == fake_vault.key_pair.fingerprint()


def test_from_env(fake_vault, monkeypatch):
    monkeypatch.setenv("NOMOS_VAULT_ADDR", "https://vault.example")
    monkeypatch.setenv("VAULT_ROLE_ID", "rid")
    monkeypatch.setenv("VAULT_SECRET_ID", "sid")
    signer = ExecutorVaultTransitSigner.from_env("nomos-executor")
    sig = signer.sign(b"x")
    assert _verify(signer.public_key_bytes(), b"x", sig.signature_bytes)


def test_from_signing_reads_attrs(fake_vault):
    signer = ExecutorVaultTransitSigner.from_signing("nomos-executor", _FakeSigning())
    sig = signer.sign(b"y")
    assert _verify(signer.public_key_bytes(), b"y", sig.signature_bytes)


def test_k8s_auth_reads_token_file(fake_vault, tmp_path):
    token_file = tmp_path / "vault-token"
    token_file.write_text("projected-sa-jwt\n")
    signer = _signer(
        role_id="", secret_id="", k8s_role="nomos-signer", k8s_token_path=str(token_file)
    )
    signer.sign(b"data")
    _method, path, body = fake_vault.requests[0]
    assert path == "auth/kubernetes/login"
    assert body == {"role": "nomos-signer", "jwt": "projected-sa-jwt"}


def test_relogin_on_403(fake_vault):
    signer = _signer()
    signer.sign(b"first")
    fake_vault.fail_first_with_403 = True
    signer.sign(b"second")
    assert fake_vault.login_count == 2


def test_no_auth_configured_fails_closed(fake_vault):
    signer = _signer(role_id="", secret_id="")
    with pytest.raises(ExecutorVaultSignerError, match="no Vault auth configured"):
        signer.sign(b"data")


def test_requires_key_id_and_addr():
    with pytest.raises(ValueError, match="key id"):
        _signer(key_id="")
    with pytest.raises(ValueError, match="NOMOS_VAULT_ADDR"):
        _signer(vault_addr="")
