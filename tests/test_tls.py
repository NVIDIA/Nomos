# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import ssl
import sys
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from nomos_runtime import (
    ClientTLSConfig,
    ServerTLSConfig,
    TLSConfigurationError,
    certificate_identity,
    listener_tls_mode,
    require_secure_url,
    require_strict_client,
    require_strict_listener,
    strict_transport_profile,
    urlopen_no_redirect,
)
from scripts.generate_dev_mtls_pki import generate


@pytest.fixture
def pki(tmp_path: Path) -> Path:
    output = tmp_path / "pki"
    generate(output)
    return output


def _pump(source: ssl.MemoryBIO, destination: ssl.MemoryBIO) -> None:
    while data := source.read():
        destination.write(data)


def _handshake(client_context: ssl.SSLContext, server_context: ssl.SSLContext, host: str) -> None:
    client_in, client_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    server_in, server_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = client_context.wrap_bio(client_in, client_out, server_hostname=host)
    server = server_context.wrap_bio(server_in, server_out, server_side=True)
    client_done = server_done = False
    for _ in range(20):
        if not client_done:
            try:
                client.do_handshake()
                client_done = True
            except ssl.SSLWantReadError:
                pass
        _pump(client_out, server_in)
        if not server_done:
            try:
                server.do_handshake()
                server_done = True
            except ssl.SSLWantReadError:
                pass
        _pump(server_out, client_in)
        if client_done and server_done:
            return
    raise AssertionError("TLS handshake did not complete")


def _client(pki: Path, *, ca: Path | None = None, cert: bool = True) -> ssl.SSLContext:
    config = ClientTLSConfig(
        enabled=True,
        ca_file=str(ca or pki / "ca.crt"),
        cert_file=str(pki / "demo.crt") if cert else "",
        key_file=str(pki / "demo.key") if cert else "",
    )
    context = config.ssl_context()
    assert isinstance(context, ssl.SSLContext)
    return context


def _server(pki: Path) -> ssl.SSLContext:
    context = ServerTLSConfig(
        enabled=True,
        cert_file=str(pki / "monitor.crt"),
        key_file=str(pki / "monitor.key"),
        client_ca_file=str(pki / "ca.crt"),
    ).ssl_context()
    assert context is not None
    return context


def _issue_server(
    pki: Path,
    name: str,
    *,
    not_before: datetime,
    not_after: datetime,
    usages: list[x509.ObjectIdentifier],
) -> None:
    ca = x509.load_pem_x509_certificate((pki / "ca.crt").read_bytes())
    ca_key = serialization.load_pem_private_key((pki / "ca.key").read_bytes(), password=None)
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "monitor")]))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("monitor")]), critical=False)
        .add_extension(x509.ExtendedKeyUsage(usages), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    (pki / f"{name}.key").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    (pki / f"{name}.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def _custom_server(pki: Path, name: str) -> ssl.SSLContext:
    context = ServerTLSConfig(
        enabled=True,
        cert_file=str(pki / f"{name}.crt"),
        key_file=str(pki / f"{name}.key"),
        client_ca_file=str(pki / "ca.crt"),
    ).ssl_context()
    assert context is not None
    return context


def test_mutual_tls_handshake_and_identity_extraction(pki: Path) -> None:
    _handshake(_client(pki), _server(pki), "monitor")
    identity = certificate_identity(str(pki / "demo.crt"))
    assert identity.uri_sans == ("spiffe://nomos.local/demo",)
    assert "demo" in identity.dns_sans
    assert len(identity.sha256_fingerprint) == 64


def test_server_rejects_missing_client_certificate(pki: Path) -> None:
    with pytest.raises(ssl.SSLError):
        _handshake(_client(pki, cert=False), _server(pki), "monitor")


def test_client_rejects_wrong_ca(pki: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    generate(other)
    with pytest.raises(ssl.SSLError):
        _handshake(_client(pki, ca=other / "ca.crt"), _server(pki), "monitor")


def test_client_rejects_hostname_mismatch(pki: Path) -> None:
    with pytest.raises(ssl.SSLError):
        _handshake(_client(pki), _server(pki), "not-monitor")


@pytest.mark.parametrize("validity", ["expired", "not-yet-valid"])
def test_client_rejects_invalid_certificate_dates(pki: Path, validity: str) -> None:
    now = datetime.now(UTC)
    if validity == "expired":
        start, end = now - timedelta(days=2), now - timedelta(days=1)
    else:
        start, end = now + timedelta(days=1), now + timedelta(days=2)
    _issue_server(
        pki,
        validity,
        not_before=start,
        not_after=end,
        usages=[ExtendedKeyUsageOID.SERVER_AUTH],
    )
    with pytest.raises(ssl.SSLError):
        _handshake(_client(pki), _custom_server(pki, validity), "monitor")


def test_client_rejects_wrong_extended_key_usage(pki: Path) -> None:
    now = datetime.now(UTC)
    _issue_server(
        pki,
        "client-only",
        not_before=now - timedelta(minutes=1),
        not_after=now + timedelta(days=1),
        usages=[ExtendedKeyUsageOID.CLIENT_AUTH],
    )
    with pytest.raises(ssl.SSLError):
        _handshake(_client(pki), _custom_server(pki, "client-only"), "monitor")


def test_ca_rotation_bundle_accepts_new_ca(pki: Path, tmp_path: Path) -> None:
    replacement = tmp_path / "replacement"
    generate(replacement)
    bundle = tmp_path / "rotation-bundle.crt"
    bundle.write_bytes((pki / "ca.crt").read_bytes() + (replacement / "ca.crt").read_bytes())
    _handshake(_client(replacement, ca=bundle), _server(replacement), "monitor")


@pytest.mark.parametrize(
    "config",
    [
        ClientTLSConfig(enabled=True, ca_file="missing"),
        ClientTLSConfig(enabled=True, ca_file="missing", cert_file="only-cert"),
        ClientTLSConfig(enabled=False, ca_file="unexpected"),
        ServerTLSConfig(enabled=True, cert_file="only-cert"),
        ServerTLSConfig(enabled=False, client_ca_file="unexpected"),
    ],
)
def test_partial_or_unreadable_configuration_fails(config: object) -> None:
    with pytest.raises(TLSConfigurationError):
        config.validated()  # type: ignore[attr-defined]


def test_plaintext_is_only_allowed_in_non_strict_mode() -> None:
    tls = ClientTLSConfig()
    require_secure_url("http://council:9901/mcp", tls=tls, label="council", strict=False)
    with pytest.raises(TLSConfigurationError, match="explicitly configured mTLS"):
        require_secure_url("http://council:9901/mcp", tls=tls, label="council", strict=True)


@pytest.mark.parametrize("profile", ["staging", "productoin"])
def test_transport_checks_reject_unknown_deployment_profiles(profile: str) -> None:
    with pytest.raises(ValueError, match="unknown deployment profile"):
        strict_transport_profile(profile)


@pytest.mark.asyncio
async def test_strict_mcp_client_refuses_https_to_http_307(
    monkeypatch: pytest.MonkeyPatch, pki: Path
) -> None:
    import httpx

    tls = ClientTLSConfig(
        enabled=True,
        ca_file=str(pki / "ca.crt"),
        cert_file=str(pki / "council-grpc.crt"),
        key_file=str(pki / "council-grpc.key"),
    )
    require_secure_url(
        "https://council:9901/mcp",
        tls=tls,
        label="COUNCIL_MCP_URL",
        strict=strict_transport_profile("production"),
    )

    requested_urls: list[str] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(
            307,
            headers={"Location": "http://attacker.invalid/mcp"},
            request=request,
        )

    transport = httpx.MockTransport(redirect)
    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: async_client(transport=transport, **kwargs),
    )

    async with tls.mcp_http_client_factory()() as client:
        response = await client.post("https://council:9901/mcp", content=b"sensitive")

    assert response.status_code == 307
    assert requested_urls == ["https://council:9901/mcp"]


def test_strict_mode_rejects_disabled_hostname_verification(pki: Path) -> None:
    tls = ClientTLSConfig(
        enabled=True,
        ca_file=str(pki / "ca.crt"),
        cert_file=str(pki / "demo.crt"),
        key_file=str(pki / "demo.key"),
        verify_hostname=False,
    )
    with pytest.raises(TLSConfigurationError, match="explicitly configured mTLS"):
        require_secure_url("https://monitor:9901", tls=tls, label="monitor", strict=True)


def test_strict_mode_rejects_https_without_client_certificate(pki: Path) -> None:
    tls = ClientTLSConfig(
        enabled=True,
        ca_file=str(pki / "ca.crt"),
        verify_hostname=True,
    )
    with pytest.raises(TLSConfigurationError, match="explicitly configured mTLS"):
        require_secure_url("https://monitor:9901", tls=tls, label="monitor", strict=True)


def test_strict_listener_requires_client_authentication(pki: Path) -> None:
    listener = ServerTLSConfig(
        enabled=True,
        cert_file=str(pki / "monitor.crt"),
        key_file=str(pki / "monitor.key"),
        require_client_cert=False,
    )
    with pytest.raises(TLSConfigurationError, match="client certificates"):
        require_strict_listener(listener, label="monitor", strict=True)


def test_strict_client_requires_verified_mutual_tls(pki: Path) -> None:
    plaintext = ClientTLSConfig()
    require_strict_client(plaintext, label="Redis", strict=False)
    with pytest.raises(TLSConfigurationError, match="Redis mTLS with hostname verification"):
        require_strict_client(plaintext, label="Redis", strict=True)

    for incomplete in (
        ClientTLSConfig(enabled=True, cert_file=str(pki / "demo.crt")),
        ClientTLSConfig(enabled=True, ca_file=str(pki / "ca.crt"), cert_file=str(pki / "demo.crt")),
    ):
        with pytest.raises(TLSConfigurationError, match="Redis mTLS with hostname verification"):
            require_strict_client(incomplete, label="Redis", strict=True)

    unverified = ClientTLSConfig(
        enabled=True,
        ca_file=str(pki / "ca.crt"),
        cert_file=str(pki / "demo.crt"),
        key_file=str(pki / "demo.key"),
        verify_hostname=False,
    )
    with pytest.raises(TLSConfigurationError, match="Redis mTLS with hostname verification"):
        require_strict_client(unverified, label="Redis", strict=True)

    verified = ClientTLSConfig(
        enabled=True,
        ca_file=str(pki / "ca.crt"),
        cert_file=str(pki / "demo.crt"),
        key_file=str(pki / "demo.key"),
    )
    require_strict_client(verified, label="Redis", strict=True)


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (ServerTLSConfig(), "plaintext development mode"),
        (ServerTLSConfig(enabled=True, require_client_cert=False), "one-way TLS"),
        (ServerTLSConfig(enabled=True, require_client_cert=True), "mutual TLS"),
    ],
)
def test_listener_tls_mode_reports_effective_client_authentication(
    config: ServerTLSConfig, expected: str
) -> None:
    assert listener_tls_mode(config) == expected


def test_generated_private_keys_are_restrictive(tmp_path: Path) -> None:
    generate(tmp_path)
    assert (tmp_path / "ca.key").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "monitor.key").stat().st_mode & 0o777 == 0o600


def test_urlopen_no_redirect_refuses_http_downgrade() -> None:
    redirected_requests = 0

    class DestinationHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal redirected_requests
            redirected_requests += 1
            self.send_response(200)
            self.end_headers()

        def log_message(self, format, *args):
            pass

    destination = HTTPServer(("127.0.0.1", 0), DestinationHandler)

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{destination.server_address[1]}/target")
            self.end_headers()

        def log_message(self, format, *args):
            pass

    redirect = HTTPServer(("127.0.0.1", 0), RedirectHandler)
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (destination, redirect)
    ]
    for thread in threads:
        thread.start()
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{redirect.server_address[1]}/")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urlopen_no_redirect(request, timeout=2)
        assert exc.value.code == 302
        assert redirected_requests == 0
    finally:
        for server in (redirect, destination):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)


def test_grpc_rejects_unsupported_tls_minimum(pki: Path) -> None:
    client = ClientTLSConfig(
        enabled=True,
        ca_file=str(pki / "ca.crt"),
        cert_file=str(pki / "demo.crt"),
        key_file=str(pki / "demo.key"),
        minimum_version="TLSv1.3",
    )
    server = ServerTLSConfig(
        enabled=True,
        cert_file=str(pki / "monitor.crt"),
        key_file=str(pki / "monitor.key"),
        client_ca_file=str(pki / "ca.crt"),
        minimum_version="TLSv1.3",
    )
    with pytest.raises(TLSConfigurationError, match="cannot enforce"):
        client.grpc_channel_credentials()
    with pytest.raises(TLSConfigurationError, match="cannot enforce"):
        server.grpc_server_credentials()


def test_grpc_accepts_alternate_tls12_spelling(pki: Path) -> None:
    client = ClientTLSConfig(
        enabled=True,
        ca_file=str(pki / "ca.crt"),
        cert_file=str(pki / "demo.crt"),
        key_file=str(pki / "demo.key"),
        minimum_version="tlsv1.2",
    )
    server = ServerTLSConfig(
        enabled=True,
        cert_file=str(pki / "monitor.crt"),
        key_file=str(pki / "monitor.key"),
        client_ca_file=str(pki / "ca.crt"),
        minimum_version="tlsv1.2",
    )
    assert client.grpc_channel_credentials() is not None
    assert server.grpc_server_credentials() is not None


def test_redis_minimum_version_resolves_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from nomos.config.settings import RedisConfig

    monkeypatch.delenv("COUNCIL_REDIS_TLS_MINIMUM_VERSION", raising=False)
    assert RedisConfig().tls_minimum_version == "TLSv1.2"
    monkeypatch.setenv("COUNCIL_REDIS_TLS_MINIMUM_VERSION", "TLSv1.3")
    assert RedisConfig().tls_minimum_version == "TLSv1.3"


def test_executor_certificate_covers_default_compose_service(pki: Path) -> None:
    identity = certificate_identity(str(pki / "executor.crt"))
    assert "executor-default" in identity.dns_sans


@pytest.mark.asyncio
async def test_opa_client_does_not_follow_redirected_decision() -> None:
    from nomos.config.settings import OPAConfig
    from nomos.opa.client import OPAEvaluationError, OPAHttpClient

    class Handler(BaseHTTPRequestHandler):
        contacted_destination = False

        def do_POST(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/allow")
            self.end_headers()

        def do_GET(self):
            Handler.contacted_destination = True
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"result":{"allow":true}}')

        def log_message(self, format, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = OPAHttpClient(
            OPAConfig(enabled=True, url=f"http://127.0.0.1:{server.server_address[1]}")
        )
        with pytest.raises(OPAEvaluationError, match="HTTP 302"):
            await client.evaluate({"intent": {}})
        assert Handler.contacted_destination is False
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_uvicorn_listener_uses_shared_mutual_tls_context(
    monkeypatch: pytest.MonkeyPatch, pki: Path
) -> None:
    import nomos_runtime.uvicorn_server as uvicorn_server

    captured: dict[str, object] = {}

    class FakeConfig:
        def __init__(self, app, **kwargs):
            self.app = app
            self.ssl = None
            captured["config"] = self

        def load(self):
            return None

    class FakeServer:
        def __init__(self, config):
            captured["server_config"] = config

        def run(self):
            return None

    fake_uvicorn = SimpleNamespace(Config=FakeConfig, Server=FakeServer)
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setattr(
        sys,
        "argv",
        ["uvicorn_server", "example:app", "--port", "9901", "--tls-prefix", "TEST"],
    )
    monkeypatch.setenv("TEST_TLS_ENABLED", "true")
    monkeypatch.setenv("TEST_TLS_CERT_FILE", str(pki / "monitor.crt"))
    monkeypatch.setenv("TEST_TLS_KEY_FILE", str(pki / "monitor.key"))
    monkeypatch.setenv("TEST_TLS_CLIENT_CA_FILE", str(pki / "ca.crt"))

    uvicorn_server.main()

    context = captured["config"].ssl
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    with pytest.raises(ssl.SSLError):
        _handshake(_client(pki, cert=False), context, "monitor")
