# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

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
    """Stands in for the signer's HTTP exchange with a Vault transit engine."""

    def __init__(self):
        self.key_pair = KeyPair.generate(key_id="vault-key")
        self.requests: list[tuple[str, str, dict | None]] = []
        self.fail_first_with_403 = False
        self.login_count = 0

    def send(self, method, path, headers, data):
        body = json.loads(data) if data else None
        path = path.split("v1/", 1)[1]
        self.requests.append((method, path, body))

        if path.startswith("auth/"):
            self.login_count += 1
            payload = {"auth": {"client_token": f"token-{self.login_count}"}}
        elif self.fail_first_with_403:
            self.fail_first_with_403 = False
            return 403, b""
        elif path.startswith("transit/sign/"):
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

            signed = base64.b64decode(body["input"])
            key = Ed25519PrivateKey.from_private_bytes(self.key_pair.private_key)
            sig = base64.b64encode(key.sign(signed)).decode()
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

        return 200, json.dumps(payload).encode()


@pytest.fixture
def fake_vault(monkeypatch):
    vault = _FakeVault()
    monkeypatch.setattr(
        VaultTransitSigner, "_send", lambda self, *args, **kwargs: vault.send(*args)
    )
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


class _CountingVault:
    """A real HTTP/1.1 server that counts connections and can drop them."""

    def __init__(self):
        import http.server
        import threading

        outer = self
        self.connections = 0
        self.requests = 0
        self.drop_after_response = False
        self.truncate_next_sign = False
        self.sign_requests = 0
        self.logins = 0

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                outer.connections += 1
                super().setup()

            def _reply(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                outer.requests += 1
                if self.path.startswith("/v1/auth/"):
                    outer.logins += 1
                    payload = {"auth": {"client_token": "t"}}
                else:
                    outer.sign_requests += 1
                    payload = {"data": {"signature": "vault:v1:" + base64.b64encode(b"s").decode()}}
                raw = json.dumps(payload).encode()
                if outer.truncate_next_sign and not self.path.startswith("/v1/auth/"):
                    outer.truncate_next_sign = False
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw) + 50))
                    self.end_headers()
                    self.wfile.write(raw[:5])
                    self.wfile.flush()
                    self.close_connection = True
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                if outer.drop_after_response:
                    self.close_connection = True

            do_POST = _reply  # noqa: N815
            do_GET = _reply  # noqa: N815

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def addr(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def counting_vault():
    vault = _CountingVault()
    yield vault
    vault.close()


def _signer_for(addr: str) -> VaultTransitSigner:
    config = SigningConfig(
        provider="vault",
        council_key_id="council-prod",
        vault_addr=addr,
        vault_role_id="role-id",
        vault_secret_id="secret-id",
    )
    return VaultTransitSigner(config, "council-prod")


class TestVaultConnectionReuse:
    def test_signs_share_one_connection(self, counting_vault):
        signer = _signer_for(counting_vault.addr)
        for i in range(5):
            signer.sign(f"payload-{i}".encode())
        assert counting_vault.requests == 6  # one login, five signs
        assert counting_vault.connections == 1

    def test_dropped_idle_connection_is_replaced_once(self, counting_vault):
        signer = _signer_for(counting_vault.addr)
        counting_vault.drop_after_response = True
        signer.sign(b"first")
        signer.sign(b"second")  # the server closed the connection; reconnect, do not fail
        assert counting_vault.connections >= 2

    def test_each_thread_has_its_own_connection(self, counting_vault):
        import threading

        signer = _signer_for(counting_vault.addr)
        signer.sign(b"main")
        thread = threading.Thread(target=signer.sign, args=(b"other",))
        thread.start()
        thread.join()
        assert counting_vault.connections == 2

    def test_concurrent_first_use_logs_in_once(self, counting_vault):
        import threading

        signer = _signer_for(counting_vault.addr)
        threads = [threading.Thread(target=signer.sign, args=(b"x",)) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert counting_vault.requests == 7  # one login, six signs

    def test_unreachable_vault_fails_closed(self):
        from nomos.crypto.signer_provider import VaultSignerError

        signer = _signer_for("http://127.0.0.1:1")
        with pytest.raises(VaultSignerError):
            signer.sign(b"x")

    def test_truncated_response_is_not_replayed(self, counting_vault):
        from nomos.crypto.signer_provider import VaultSignerError

        signer = _signer_for(counting_vault.addr)
        signer.sign(b"first")  # connection is now a reused one
        counting_vault.truncate_next_sign = True
        with pytest.raises(VaultSignerError):
            signer.sign(b"second")
        assert counting_vault.sign_requests == 2  # the failed sign reached Vault once
        signer.sign(b"third")  # and the signer recovers on a new connection
        assert counting_vault.sign_requests == 3

    def test_login_is_never_sent_on_a_reused_connection_or_repeated(self, counting_vault):
        signer = _signer_for(counting_vault.addr)
        signer.sign(b"first")
        counting_vault.drop_after_response = True
        signer.sign(b"second")
        signer._token = None  # force a login while the kept-alive connection is stale
        signer.sign(b"third")
        assert counting_vault.logins == 2

    def test_redirect_is_rejected(self):
        import http.server
        import threading

        from nomos.crypto.signer_provider import VaultSignerError

        class Redirect(http.server.BaseHTTPRequestHandler):
            def _go(self):
                self.send_response(307)
                self.send_header("Location", "http://127.0.0.1:1/elsewhere")
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST = _go  # noqa: N815

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            signer = _signer_for(f"http://127.0.0.1:{server.server_address[1]}")
            with pytest.raises(VaultSignerError, match="307"):
                signer.sign(b"x")
        finally:
            server.shutdown()
            server.server_close()

    def test_reset_while_reading_the_body_is_not_replayed(self, monkeypatch):
        from nomos.crypto.signer_provider import VaultSignerError

        requests: list[str] = []

        class _Response:
            status = 200

            def __init__(self, body: bytes, fail: bool):
                self._body, self._fail = body, fail

            def read(self) -> bytes:
                if self._fail:
                    raise ConnectionResetError("reset after headers")
                return self._body

        class _Connection:
            fail_sign_body = False

            def __init__(self, *args, **kwargs):
                self._path = ""

            def request(self, method, target, body=None, headers=None):
                self._path = target
                requests.append(target)

            def getresponse(self):
                if "/auth/" in self._path:
                    return _Response(json.dumps({"auth": {"client_token": "t"}}).encode(), False)
                sig = base64.b64encode(b"s").decode()
                body = json.dumps({"data": {"signature": f"vault:v1:{sig}"}}).encode()
                return _Response(body, _Connection.fail_sign_body)

            def close(self):
                pass

        monkeypatch.setattr("http.client.HTTPConnection", _Connection)
        signer = _signer_for("http://vault.invalid:8200")
        signer.sign(b"first")  # leaves a reused connection behind
        signs_before = sum("/sign/" in r for r in requests)
        _Connection.fail_sign_body = True
        with pytest.raises(VaultSignerError):
            signer.sign(b"second")
        assert sum("/sign/" in r for r in requests) == signs_before + 1
