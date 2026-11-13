"""Actual local socket/TLS/proxy compatibility for the pooled OPA client."""

import asyncio
import datetime
import ssl

import httpcore
import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from nomos.config.settings import OPAConfig
from nomos.opa.client import OPAEvaluationError, OPAHttpClient


@pytest.fixture(autouse=True)
def clean_proxy_environment(monkeypatch):
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    ):
        monkeypatch.delenv(key, raising=False)


async def _respond(reader, writer, seen=None):
    try:
        head = await reader.readuntil(b"\r\n\r\n")
        if seen is not None:
            seen.append(head)
        body = b'{"result":true}'
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_environment_proxy_and_no_proxy_are_honored(monkeypatch):
    seen = []
    proxy = await asyncio.start_server(lambda r, w: _respond(r, w, seen), "127.0.0.1", 0)
    port = proxy.sockets[0].getsockname()[1]
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{port}")
    client = OPAHttpClient(OPAConfig(url="http://127.0.0.1:1"))
    try:
        assert (await client.evaluate({})).allow
        assert seen[0].startswith(b"POST http://127.0.0.1:1/")
    finally:
        await client.aclose()
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    direct = OPAHttpClient(OPAConfig(url="http://127.0.0.1:1"))
    try:
        with pytest.raises(OPAEvaluationError) as failure:
            await direct.evaluate({})
        assert isinstance(failure.value.__cause__, httpx.ConnectError)
        assert len(seen) == 1
    finally:
        await direct.aclose()
        proxy.close()
        await proxy.wait_closed()


@pytest.mark.asyncio
async def test_tls_rejects_untrusted_and_hostname_mismatch_but_honors_ca_file(
    tmp_path, monkeypatch
):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    certpath, keypath = tmp_path / "ca.pem", tmp_path / "key.pem"
    certpath.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keypath.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certpath, keypath)
    server = await asyncio.start_server(_respond, "127.0.0.1", 0, ssl=context)
    port = server.sockets[0].getsockname()[1]
    try:
        for host, trusted, succeeds in (
            ("localhost", False, False),
            ("localhost", True, True),
            ("127.0.0.1", True, False),
        ):
            if trusted:
                monkeypatch.setenv("SSL_CERT_FILE", str(certpath))
            client = OPAHttpClient(OPAConfig(url=f"https://{host}:{port}"))
            try:
                if succeeds:
                    assert (await client.evaluate({})).allow
                else:
                    with pytest.raises(OPAEvaluationError) as failure:
                        await client.evaluate({})
                    assert isinstance(failure.value.__cause__, httpx.ConnectError)
            finally:
                await client.aclose()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("send_headers", [False, True])
async def test_real_slow_headers_and_body_hit_read_timeout(send_headers):
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    calls = []

    async def slow(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            calls.append(True)
            if send_headers:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 15\r\n\r\n")
                await writer.drain()
            entered.set()
            await release.wait()
        finally:
            writer.close()
            await writer.wait_closed()
            finished.set()

    server = await asyncio.start_server(slow, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = OPAHttpClient(OPAConfig(url=f"http://127.0.0.1:{port}", timeout_ms=20))
    try:
        with pytest.raises(OPAEvaluationError) as failure:
            await client.evaluate({})
        assert entered.is_set() and len(calls) == 1
        assert isinstance(failure.value.__cause__, httpx.ReadTimeout)
    finally:
        release.set()
        await finished.wait()
        await client.aclose()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_connect_budget_reaches_network_backend_without_retry(monkeypatch):
    calls = []

    async def connect(self, host, port, timeout=None, **kwargs):
        calls.append(timeout)
        raise httpcore.ConnectTimeout("controlled local test")

    from httpcore._backends.auto import AutoBackend

    monkeypatch.setattr(AutoBackend, "connect_tcp", connect)
    client = OPAHttpClient(OPAConfig(url="http://127.0.0.1:1", timeout_ms=20))
    try:
        with pytest.raises(OPAEvaluationError) as failure:
            await client.evaluate({})
        assert isinstance(failure.value.__cause__, httpx.ConnectTimeout)
        assert calls == [0.02]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_uses_system_ssl_context_factory(monkeypatch):
    original_context = ssl.create_default_context()
    calls = []
    real_client = httpx.AsyncClient

    def construct(**kwargs):
        calls.append(kwargs)
        return real_client(**kwargs)

    monkeypatch.setattr("nomos.opa.client.ssl.create_default_context", lambda: original_context)
    monkeypatch.setattr("nomos.opa.client.httpx.AsyncClient", construct)
    client = OPAHttpClient(OPAConfig())
    try:
        client._http_client()
        assert calls[0]["verify"] is original_context
        assert original_context.verify_mode == ssl.CERT_REQUIRED
        assert original_context.check_hostname
        assert calls[0]["trust_env"] is True
    finally:
        await client.aclose()
