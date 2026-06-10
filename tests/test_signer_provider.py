"""Tests for signer providers (local, in-memory, Vault transit)."""

from __future__ import annotations

import base64
import json

import pytest

from nomos.config.settings import SigningConfig
from nomos.crypto.signer_provider import (
    InMemorySigner,
    LocalFileSigner,
    SignerProvider,
    VaultSignerError,
    VaultTransitSigner,
    create_signer,
)
from nomos.level_0.crypto.signing import KeyPair, Verifier


class TestInMemorySigner:
    def test_signs_and_verifies(self):
        signer = InMemorySigner()
        sig = signer.sign(b"payload")
        verifier = Verifier()
        verifier.register_public_key(signer.key_id, signer.public_key_bytes())
        assert verifier.verify(b"payload", sig)

    def test_conforms_to_protocol(self):
        assert isinstance(InMemorySigner(), SignerProvider)

    def test_wraps_existing_key_pair(self):
        kp = KeyPair.generate(key_id="council-test")
        signer = InMemorySigner(kp)
        assert signer.key_id == "council-test"
        assert signer.public_key_bytes() == kp.public_key
        assert signer.fingerprint() == kp.fingerprint()


class TestLocalFileSigner:
    def test_loads_or_generates_and_signs(self, tmp_path):
        signer = LocalFileSigner(str(tmp_path / "keys"))
        sig = signer.sign(b"data")
        verifier = Verifier()
        verifier.register_public_key(signer.key_id, signer.public_key_bytes())
        assert verifier.verify(b"data", sig)
        assert isinstance(signer, SignerProvider)

    def test_stable_across_instances(self, tmp_path):
        a = LocalFileSigner(str(tmp_path / "keys"))
        b = LocalFileSigner(str(tmp_path / "keys"))
        assert a.public_key_bytes() == b.public_key_bytes()
        assert a.fingerprint() == b.fingerprint()


class _FakeVault:
    """Stands in for urllib.request.urlopen against a Vault transit engine."""

    def __init__(self):
        self.key_pair = KeyPair.generate(key_id="vault-key")
        self.requests: list[tuple[str, str, dict | None]] = []
        self.fail_first_with_403 = False
        self.login_count = 0

    def urlopen(self, request, timeout=0):
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
    monkeypatch.setattr("urllib.request.urlopen", vault.urlopen)
    return vault


def _vault_config() -> SigningConfig:
    return SigningConfig(
        provider="vault",
        council_key_id="council-prod",
        vault_addr="https://vault.example",
        vault_role_id="role-id",
        vault_secret_id="secret-id",
    )


class TestVaultTransitSigner:
    def test_signs_via_transit_and_verifies(self, fake_vault):
        signer = VaultTransitSigner(_vault_config(), "council-prod")
        sig = signer.sign(b"writ canonical bytes")

        verifier = Verifier()
        verifier.register_public_key("council-prod", signer.public_key_bytes())
        assert verifier.verify(b"writ canonical bytes", sig)
        assert sig.signer_id == "council-prod"
        assert signer.fingerprint() == fake_vault.key_pair.fingerprint()
        assert isinstance(signer, SignerProvider)

    def test_relogin_on_403(self, fake_vault):
        signer = VaultTransitSigner(_vault_config(), "council-prod")
        signer.sign(b"first")  # login 1
        fake_vault.fail_first_with_403 = True
        signer.sign(b"second")  # 403 -> login 2 -> retry
        assert fake_vault.login_count == 2

    def test_no_auth_configured_fails_closed(self, fake_vault):
        config = SigningConfig(
            provider="vault",
            council_key_id="council-prod",
            vault_addr="https://vault.example",
        )
        signer = VaultTransitSigner(config, "council-prod")
        with pytest.raises(VaultSignerError, match="no Vault auth configured"):
            signer.sign(b"data")

    def test_requires_key_id(self):
        with pytest.raises(ValueError, match="key id"):
            VaultTransitSigner(_vault_config(), "")


class TestCreateSigner:
    def test_local_with_key_dir(self, tmp_path):
        config = SigningConfig(provider="local")
        signer = create_signer(config, key_id="x", local_key_dir=str(tmp_path / "keys"))
        assert isinstance(signer, LocalFileSigner)

    def test_local_without_key_dir_is_ephemeral(self):
        signer = create_signer(SigningConfig(provider="local"), key_id="x")
        assert isinstance(signer, InMemorySigner)

    def test_vault(self, fake_vault):
        signer = create_signer(_vault_config(), key_id="council-prod")
        assert isinstance(signer, VaultTransitSigner)


class TestSigningConfig:
    def test_vault_requires_addr_and_key(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="NOMOS_VAULT_ADDR"):
            SigningConfig(provider="vault", council_key_id="k")
        with pytest.raises(ValidationError, match="COUNCIL_SIGNER_KEY_ID"):
            SigningConfig(provider="vault", vault_addr="https://vault.example")

    def test_defaults(self, monkeypatch):
        for var in ("NOMOS_SIGNER_PROVIDER", "COUNCIL_SIGNER_KEY_ID", "NOMOS_VAULT_ADDR"):
            monkeypatch.delenv(var, raising=False)
        config = SigningConfig()
        assert config.provider == "local"
        assert config.vault_transit_mount == "transit"
        assert config.vault_k8s_token_path == "/var/run/secrets/nomos/vault-token"
