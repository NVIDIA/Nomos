# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The council's kept-open clients honour the per-link TLS settings.

The policy client, the Vault signer and the telemetry publisher each keep
connections open instead of opening one per request. Each test here talks to a
real TLS listener that requires a client certificate, so a client that ignored
``<PREFIX>_TLS_*`` would fail the handshake instead of passing a mock.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from nomos.config.settings import CouncilSettings, OPAConfig, SigningConfig
from nomos.crypto.signer_provider import VaultSignerError, VaultTransitSigner
from nomos.nat.functions import CouncilTelemetryPublisher
from nomos.opa.client import OPAEvaluationError, OPAHttpClient
from nomos_runtime import ServerTLSConfig
from scripts.generate_dev_mtls_pki import generate


class _Listener:
    """HTTPS on 127.0.0.1 that accepts only clients holding a certificate from the test CA."""

    def __init__(self, pki: Path) -> None:
        listener = self
        self.requests: list[tuple[str, str, dict[str, str], bytes]] = []
        self.client_names: list[str] = []
        self.connections = 0

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self) -> None:
                super().setup()
                listener.connections += 1

            def _answer(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                listener.requests.append((self.command, self.path, dict(self.headers), body))
                subject = dict(item[0] for item in self.connection.getpeercert()["subject"])
                listener.client_names.append(subject["commonName"])
                answer = b'{"result":{"allow":true}}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)

            def do_GET(self) -> None:
                self._answer()

            def do_POST(self) -> None:
                self._answer()

            def log_message(self, format: str, *args: object) -> None:
                return

        context = ServerTLSConfig(
            enabled=True,
            cert_file=str(pki / "monitor.crt"),
            key_file=str(pki / "monitor.key"),
            client_ca_file=str(pki / "ca.crt"),
        ).ssl_context()
        assert context is not None
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
        self.url = f"https://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)
        self._server.server_close()


@pytest.fixture
def pki(tmp_path: Path) -> Path:
    output = tmp_path / "pki"
    generate(output)
    return output


@pytest.fixture
def listener(pki: Path):
    server = _Listener(pki)
    try:
        yield server
    finally:
        server.close()


def _configure(monkeypatch: pytest.MonkeyPatch, prefix: str, pki: Path, *, identity: bool) -> None:
    monkeypatch.setenv(f"{prefix}_TLS_ENABLED", "true")
    monkeypatch.setenv(f"{prefix}_TLS_CA_FILE", str(pki / "ca.crt"))
    if identity:
        monkeypatch.setenv(f"{prefix}_TLS_CERT_FILE", str(pki / "council.crt"))
        monkeypatch.setenv(f"{prefix}_TLS_KEY_FILE", str(pki / "council.key"))


@pytest.mark.asyncio
async def test_policy_client_presents_its_certificate_and_reuses_the_connection(
    monkeypatch, pki, listener
):
    _configure(monkeypatch, "COUNCIL_OPA", pki, identity=True)
    client = OPAHttpClient(OPAConfig(enabled=True, url=listener.url, timeout_ms=5000))
    try:
        first = await client.evaluate({"intent": {}})
        second = await client.evaluate({"intent": {}})
    finally:
        await client.aclose()

    assert first.allow and second.allow
    assert listener.client_names == ["council", "council"]
    assert listener.connections == 1


@pytest.mark.asyncio
async def test_policy_client_without_a_certificate_is_refused(monkeypatch, pki, listener):
    _configure(monkeypatch, "COUNCIL_OPA", pki, identity=False)
    client = OPAHttpClient(OPAConfig(enabled=True, url=listener.url, timeout_ms=5000))
    try:
        with pytest.raises(OPAEvaluationError):
            await client.evaluate({"intent": {}})
    finally:
        await client.aclose()

    assert listener.requests == []


@pytest.mark.asyncio
async def test_policy_client_does_not_trust_the_private_ca_unless_configured(listener):
    client = OPAHttpClient(OPAConfig(enabled=True, url=listener.url, timeout_ms=5000))
    try:
        with pytest.raises(OPAEvaluationError):
            await client.evaluate({"intent": {}})
    finally:
        await client.aclose()

    assert listener.requests == []


def _signer(url: str) -> VaultTransitSigner:
    config = SigningConfig(
        provider="vault",
        council_key_id="council-prod",
        vault_addr=url,
        vault_role_id="role-id",
        vault_secret_id="secret-id",
    )
    return VaultTransitSigner(config, "council-prod")


def test_vault_signer_presents_its_certificate_on_a_kept_alive_connection(
    monkeypatch, pki, listener
):
    _configure(monkeypatch, "NOMOS_VAULT", pki, identity=True)
    signer = _signer(listener.url)

    for _ in range(2):
        assert signer._request("GET", "v1/sys/health", None, token="vault-token") == {
            "result": {"allow": True}
        }

    assert listener.client_names == ["council", "council"]
    assert listener.connections == 1
    assert all(request[2]["X-Vault-Token"] == "vault-token" for request in listener.requests)


def test_vault_signer_without_a_certificate_is_refused(monkeypatch, pki, listener):
    _configure(monkeypatch, "NOMOS_VAULT", pki, identity=False)
    signer = _signer(listener.url)

    with pytest.raises(VaultSignerError):
        signer._request("GET", "v1/sys/health", None, token="vault-token")

    assert listener.requests == []


def test_vault_signer_does_not_trust_the_private_ca_unless_configured(listener):
    signer = _signer(listener.url)

    with pytest.raises(VaultSignerError):
        signer._request("GET", "v1/sys/health", None, token="vault-token")

    assert listener.requests == []


def _publisher(url: str) -> CouncilTelemetryPublisher:
    return CouncilTelemetryPublisher(
        CouncilSettings(
            telemetry={
                "enabled": True,
                "url": f"{url}/internal/council-latency",
                "token": "internal-token",
                "redis_enabled": False,
            }
        )
    )


@pytest.mark.asyncio
async def test_telemetry_publisher_presents_its_certificate(monkeypatch, pki, listener):
    _configure(monkeypatch, "COUNCIL_TELEMETRY", pki, identity=True)
    publisher = _publisher(listener.url)
    try:
        await publisher._post({"stage": "review", "ms": 12})
        await publisher._post({"stage": "ledger", "ms": 3})
    finally:
        await publisher.aclose()

    assert listener.client_names == ["council", "council"]
    assert listener.connections == 1
    method, path, headers, body = listener.requests[0]
    assert (method, path) == ("POST", "/internal/council-latency")
    assert headers["X-Council-Internal-Token"] == "internal-token"
    assert json.loads(body) == {"stage": "review", "ms": 12}


@pytest.mark.asyncio
async def test_telemetry_publisher_without_a_certificate_is_refused(monkeypatch, pki, listener):
    _configure(monkeypatch, "COUNCIL_TELEMETRY", pki, identity=False)
    publisher = _publisher(listener.url)
    try:
        with pytest.raises(Exception):  # noqa: B017 - any transport failure; none may deliver
            await publisher._post({"stage": "review", "ms": 12})
    finally:
        await publisher.aclose()

    assert listener.requests == []
