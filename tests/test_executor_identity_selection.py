"""NOMOS-3: executor evidence identity selects Vault transit vs local file keys."""

from __future__ import annotations

import sys
import types

from executors.openshell.identity import build_executor_identity


class _FakeSigningConfig:
    def __init__(self, provider: str):
        self.provider = provider
        self.vault_addr = "http://vault:8200"
        self.vault_transit_mount = "transit"
        self.vault_k8s_role = "nomos-signer"
        self.vault_k8s_token_path = "/var/run/secrets/nomos/vault-token"
        self.vault_namespace = ""


def test_local_provider_uses_file_keys(tmp_path):
    # Empty path → in-memory ephemeral key; returns a local ExecutorIdentity.
    identity = build_executor_identity(
        private_key_path="", public_key_path="", signing=_FakeSigningConfig("local")
    )
    assert hasattr(identity, "private_key")  # ExecutorIdentity, not provider-backed
    assert len(identity.key_fingerprint) == 64


def test_vault_provider_builds_transit_signer_identity(monkeypatch):
    monkeypatch.setenv("EXECUTOR_SIGNER_KEY_ID", "nomos-executor")

    captured = {}

    class _FakeSigner:
        def __init__(self, config, key_id):
            captured["key_id"] = key_id
            captured["addr"] = config.vault_addr
            self.key_id = key_id

        def public_key_bytes(self):
            return b"\x00" * 32

    # Inject a fake VaultTransitSigner so no live Vault is needed.
    fake_mod = types.ModuleType("nomos.crypto.signer_provider")
    fake_mod.VaultTransitSigner = _FakeSigner
    monkeypatch.setitem(sys.modules, "nomos.crypto.signer_provider", fake_mod)

    identity = build_executor_identity(signing=_FakeSigningConfig("vault"))

    # Provider-backed identity (no private_key bytes), signs via the signer.
    assert not hasattr(identity, "private_key")
    assert identity.key_id == "nomos-executor"
    assert captured["key_id"] == "nomos-executor"
    assert captured["addr"] == "http://vault:8200"
    assert len(identity.key_fingerprint) == 64
