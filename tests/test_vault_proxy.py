# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Vault requests follow the environment's proxy settings, as urllib did.

Every server here listens on the loopback interface; nothing leaves the host.
"""

from __future__ import annotations

import base64
import socket
import threading
from collections.abc import Iterator

import pytest

from nomos.crypto.signer_provider import (
    SigningConfig,
    VaultSignerError,
    VaultTransitSigner,
)

_PROXY_VARIABLES = (
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "REQUEST_METHOD",
)


@pytest.fixture(autouse=True)
def _no_inherited_proxy(monkeypatch):
    for name in _PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)


class _Server:
    """Loopback HTTP endpoint that records each request head and answers ``{}``.

    ``tunnel`` makes it answer a CONNECT the way a proxy does and then hang up,
    which is all a test needs to see where the client went and what it sent.
    """

    def __init__(self, *, tunnel: bool = False) -> None:
        self._tunnel = tunnel
        self.heads: list[str] = []
        self.connections = 0
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._talk, args=(client,), daemon=True).start()

    def _talk(self, client: socket.socket) -> None:
        with client:
            buffer = b""
            while True:
                while b"\r\n\r\n" not in buffer:
                    chunk = client.recv(65536)
                    if not chunk:
                        return
                    buffer += chunk
                head, _, buffer = buffer.partition(b"\r\n\r\n")
                self.heads.append(head.decode())
                if self._tunnel:
                    client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                    return
                client.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")

    def close(self) -> None:
        self._listener.close()

    def request_lines(self) -> list[str]:
        return [head.split("\r\n")[0] for head in self.heads]

    def header(self, index: int, name: str) -> str | None:
        for line in self.heads[index].split("\r\n")[1:]:
            key, _, value = line.partition(": ")
            if key.lower() == name.lower():
                return value
        return None


@pytest.fixture
def server() -> Iterator[_Server]:
    endpoint = _Server()
    yield endpoint
    endpoint.close()


@pytest.fixture
def tunnel_proxy() -> Iterator[_Server]:
    endpoint = _Server(tunnel=True)
    yield endpoint
    endpoint.close()


def _signer(vault_addr: str) -> VaultTransitSigner:
    return VaultTransitSigner(
        SigningConfig(provider="vault", vault_addr=vault_addr, council_key_id="test", timeout_s=3),
        "test",
    )


def _tunnel_requests(proxy: _Server) -> list[str]:
    """Method and destination of each request the proxy saw.

    The protocol version is left out: http.client asks for a tunnel as HTTP/1.0
    before Python 3.12 and as HTTP/1.1 from it.
    """
    return [line.rsplit(" ", 1)[0] for line in proxy.request_lines()]


def _unused_port() -> int:
    with socket.create_server(("127.0.0.1", 0)) as probe:
        return probe.getsockname()[1]


def test_http_vault_is_requested_through_the_environment_proxy(monkeypatch, server):
    # pragma: allowlist nextline secret
    monkeypatch.setenv("HTTP_PROXY", f"http://proxy-user:p%40ss@127.0.0.1:{server.port}")
    signer = _signer("http://vault.invalid:8200")
    try:
        assert signer._request("GET", "v1/sys/health", None, token="vault-token") == {}
        assert signer._request("GET", "v1/sys/health", None, token="vault-token") == {}
    finally:
        signer._close_connection()

    # The proxy is given the full URL and the Vault host, on one kept-alive connection.
    assert server.request_lines() == ["GET http://vault.invalid:8200/v1/sys/health HTTP/1.1"] * 2
    assert server.header(0, "Host") == "vault.invalid:8200"
    assert server.header(0, "X-Vault-Token") == "vault-token"
    expected = base64.b64encode(b"proxy-user:p@ss").decode()
    assert server.header(0, "Proxy-Authorization") == f"Basic {expected}"
    assert server.connections == 1


def test_https_vault_is_reached_through_a_connect_tunnel(monkeypatch, tunnel_proxy):
    # pragma: allowlist nextline secret
    monkeypatch.setenv("HTTPS_PROXY", f"http://proxy-user:pw@127.0.0.1:{tunnel_proxy.port}")
    signer = _signer("https://vault.invalid:8200")
    try:
        # The stand-in proxy accepts the tunnel and hangs up, so TLS to Vault fails.
        with pytest.raises(VaultSignerError):
            signer._request("GET", "v1/sys/health", None, token="vault-token")
    finally:
        signer._close_connection()

    assert _tunnel_requests(tunnel_proxy) == ["CONNECT vault.invalid:8200"]
    assert tunnel_proxy.header(0, "Proxy-Authorization") is not None
    # The token travels only inside the tunnel, never to the proxy.
    assert "vault-token" not in tunnel_proxy.heads[0]


def test_default_https_port_is_named_in_the_tunnel_request(monkeypatch, tunnel_proxy):
    monkeypatch.setenv("https_proxy", f"127.0.0.1:{tunnel_proxy.port}")  # bare host:port form
    signer = _signer("https://vault.invalid")
    try:
        with pytest.raises(VaultSignerError):
            signer._request("GET", "v1/sys/health", None, token="vault-token")
    finally:
        signer._close_connection()

    assert _tunnel_requests(tunnel_proxy) == ["CONNECT vault.invalid:443"]
    assert tunnel_proxy.header(0, "Proxy-Authorization") is None


@pytest.mark.parametrize("no_proxy", ["127.0.0.1", "*", "localhost,127.0.0.1"])
def test_no_proxy_sends_the_request_straight_to_vault(monkeypatch, server, no_proxy):
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{_unused_port()}")
    monkeypatch.setenv("NO_PROXY", no_proxy)
    signer = _signer(f"http://127.0.0.1:{server.port}")
    try:
        assert signer._request("GET", "v1/sys/health", None, token="vault-token") == {}
    finally:
        signer._close_connection()

    assert server.request_lines() == ["GET /v1/sys/health HTTP/1.1"]
    assert server.header(0, "Proxy-Authorization") is None


def test_without_proxy_settings_the_request_is_direct(server):
    signer = _signer(f"http://127.0.0.1:{server.port}")
    try:
        assert signer._request("GET", "v1/sys/health", None, token="vault-token") == {}
    finally:
        signer._close_connection()

    assert server.request_lines() == ["GET /v1/sys/health HTTP/1.1"]


def test_a_proxy_for_the_other_scheme_is_not_used(monkeypatch, server):
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{_unused_port()}")
    signer = _signer(f"http://127.0.0.1:{server.port}")
    try:
        assert signer._request("GET", "v1/sys/health", None, token="vault-token") == {}
    finally:
        signer._close_connection()

    assert server.request_lines() == ["GET /v1/sys/health HTTP/1.1"]
