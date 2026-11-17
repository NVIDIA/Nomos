# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for signer providers (local, in-memory, Vault transit)."""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import ssl
import threading
import time
import urllib.parse

import pytest

from nomos.auth.signing_executor import WritSigningExecutor
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
from scripts.generate_dev_mtls_pki import generate

# The Vault tests build the signer's connection, and some reach a loopback
# server through it; the signer reads proxy settings from the environment, so
# an inherited proxy would be sent the request, or refused outright.
from tests.test_vault_proxy import _no_inherited_proxy as _no_inherited_proxy


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
        self.contexts: list[ssl.SSLContext | None] = []
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
def fake_vault(monkeypatch, tmp_path):
    pki = tmp_path / "pki"
    generate(pki)
    monkeypatch.setenv("NOMOS_VAULT_TLS_ENABLED", "true")
    monkeypatch.setenv("NOMOS_VAULT_TLS_CA_FILE", str(pki / "ca.crt"))
    monkeypatch.setenv("NOMOS_VAULT_TLS_CERT_FILE", str(pki / "council.crt"))
    monkeypatch.setenv("NOMOS_VAULT_TLS_KEY_FILE", str(pki / "council.key"))
    vault = _FakeVault()

    def send(self, method, path, headers, data, *, replay_safe=True):
        # Build the connection the signer would use, without opening it, so the
        # tests can see which TLS context a request to Vault is made with.
        connection = self._connect(urllib.parse.urlsplit(self._config.vault_addr))
        vault.contexts.append(getattr(connection, "_context", None))
        connection.close()
        return vault.send(method, path, headers, data)

    monkeypatch.setattr(VaultTransitSigner, "_send", send)
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
        assert fake_vault.contexts
        assert all(isinstance(context, ssl.SSLContext) for context in fake_vault.contexts)
        # The configured context, not the default trust store: one CA, and a
        # server certificate is required and checked against the host name.
        for context in fake_vault.contexts:
            assert len(context.get_ca_certs()) == 1
            assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname

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

    def test_strict_profile_rejects_insecure_vault_before_request(self, fake_vault, monkeypatch):
        monkeypatch.setenv("NOMOS_DEPLOY_PROFILE", "production")
        config = SigningConfig(
            provider="vault",
            council_key_id="council-prod",
            vault_addr="http://vault.example",
            vault_role_id="role-id",
            vault_secret_id="secret-id",
        )

        with pytest.raises(ValueError, match="must use https"):
            VaultTransitSigner(config, "council-prod")

        assert fake_vault.requests == []
        assert fake_vault.contexts == []


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


_SIGNED = json.dumps({"data": {"signature": "vault:v1:" + base64.b64encode(b"s").decode()}})


def _read_request(client: socket.socket) -> None:
    request = b""
    while b"\r\n\r\n" not in request:
        request += client.recv(65536)
    head, _, body = request.partition(b"\r\n\r\n")
    length = int(head.lower().split(b"content-length: ")[1].split(b"\r\n")[0])
    while len(body) < length:
        body += client.recv(65536)


class _TricklingVault:
    """Loopback Vault whose first response never completes.

    The first connection is sent ``head`` and then ``drip`` every ``interval``
    seconds for as long as the client stays; ``limit`` seconds ends it anyway,
    so a client with no deadline fails its test instead of hanging it. Later
    connections are answered with a complete signature.
    """

    def __init__(self, head: bytes, drip: bytes, *, interval: float, limit: float):
        self._head, self._drip, self._interval, self._limit = head, drip, interval, limit
        self.connections = 0
        self.dripping = threading.Event()
        self.hung_up = threading.Event()
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.addr = f"http://127.0.0.1:{self._listener.getsockname()[1]}"
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(
                target=self._talk, args=(client, self.connections == 1), daemon=True
            ).start()

    def _talk(self, client: socket.socket, trickle: bool) -> None:
        with client:
            _read_request(client)
            if not trickle:
                reply = f"HTTP/1.1 200 OK\r\nContent-Length: {len(_SIGNED)}\r\n\r\n{_SIGNED}"
                client.sendall(reply.encode())
                return
            client.sendall(self._head)
            self.dripping.set()
            ends = time.monotonic() + self._limit
            try:
                while time.monotonic() < ends:
                    time.sleep(self._interval)
                    client.sendall(self._drip)
            except OSError:
                self.hung_up.set()

    def close(self) -> None:
        self._listener.close()


# Where a response can stall: each is the bytes sent at once, then the bytes dripped.
_TRICKLES = {
    "headers": (b"HTTP/1.1 200 OK\r\nX-Slow: ", b"a"),
    "sized body": (b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n\r\n", b" "),
    "chunked body": (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n", b"1\r\n \r\n"),
}


class TestVaultExchangeDeadline:
    """A response that keeps arriving, but never ends, is given up on."""

    TIMEOUT_S = 0.25
    # One allowance to send the request and get the first byte, one for the rest.
    DEADLINE_S = 2 * TIMEOUT_S

    def _vault(self, shape: str) -> _TricklingVault:
        # Every byte arrives well inside the socket timeout, so that never fires.
        return _TricklingVault(
            *_TRICKLES[shape], interval=self.TIMEOUT_S / 5, limit=12 * self.TIMEOUT_S
        )

    def _signer(self, addr: str) -> VaultTransitSigner:
        config = SigningConfig(
            provider="vault",
            council_key_id="council-prod",
            vault_addr=addr,
            timeout_s=self.TIMEOUT_S,
        )
        signer = VaultTransitSigner(config, "council-prod")
        signer._token = "t"  # already logged in: the exchange under test is the sign
        return signer

    @pytest.mark.parametrize("shape", list(_TRICKLES))
    def test_trickled_response_ends_at_the_deadline(self, shape):
        vault = self._vault(shape)
        signer = self._signer(vault.addr)
        try:
            started = time.monotonic()
            with pytest.raises(VaultSignerError) as raised:
                signer.sign(b"payload")
            elapsed = time.monotonic() - started
            assert self.TIMEOUT_S < elapsed < self.DEADLINE_S + 1
            assert isinstance(raised.value.__cause__, TimeoutError)
            # The connection is closed and forgotten, and the next sign opens another.
            assert signer._local.connection is None
            assert vault.hung_up.wait(1)
            assert signer.sign(b"payload").signature_bytes == b"s"
            assert vault.connections == 2
        finally:
            vault.close()

    @pytest.mark.asyncio
    async def test_cancelled_sign_gives_its_worker_back_at_the_deadline(self):
        vault = self._vault("sized body")
        signer = self._signer(vault.addr)
        lane = WritSigningExecutor(workers=1, capacity=1)
        try:
            signing = asyncio.create_task(lane.sign(signer, b"payload"))
            while not vault.dripping.is_set():
                await asyncio.sleep(0.01)
            # A cancelled sign keeps its worker and its admission until Vault's
            # answer is in, so both are held for as long as the exchange lasts.
            signing.cancel()
            started = time.monotonic()
            with pytest.raises(asyncio.CancelledError):
                await signing
            assert time.monotonic() - started < self.DEADLINE_S + 1
            assert lane._admitted == 0
            # The only worker and the only admission are free again.
            assert (await lane.sign(signer, b"payload")).signature_bytes == b"s"
            await asyncio.wait_for(lane.aclose(), timeout=1)
        finally:
            vault.close()

    def test_response_that_ends_its_connection_is_read_to_the_end(self):
        # With no Content-Length the body ends where Vault closes the connection,
        # and the client closes its own side as soon as the headers say so. The
        # body, sent after that, must still be read.
        listener = socket.create_server(("127.0.0.1", 0))

        def answer() -> None:
            client, _ = listener.accept()
            with client:
                _read_request(client)
                client.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n")
                time.sleep(self.TIMEOUT_S / 5)
                client.sendall(_SIGNED.encode())

        threading.Thread(target=answer, daemon=True).start()
        try:
            signer = self._signer(f"http://127.0.0.1:{listener.getsockname()[1]}")
            assert signer.sign(b"payload").signature_bytes == b"s"
        finally:
            listener.close()
