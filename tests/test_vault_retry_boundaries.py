"""Real stdlib HTTP parsing with deterministic sockets; no network requests."""

import http.client
import io
from unittest.mock import patch

import pytest

from nomos.crypto.signer_provider import SigningConfig, VaultTransitSigner

OK = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"


class FaultyFile(io.BytesIO):
    def __init__(self, wire, *, header_error=None, body_error=None):
        super().__init__(wire)
        self.header_error = header_error
        self.body_error = body_error
        self.lines = 0

    def readline(self, *args):
        self.lines += 1
        if self.lines > 1 and self.header_error is not None:
            raise self.header_error
        return super().readline(*args)

    def read(self, *args):
        if self.body_error is not None:
            raise self.body_error
        return super().read(*args)


class Socket:
    def __init__(self, wire=OK, *, send_error=None, **file_options):
        self.file = FaultyFile(wire, **file_options)
        self.send_error = send_error
        self.requests = 0
        self.closed = False

    def sendall(self, data):
        if bytes(data).startswith(b"POST "):
            self.requests += 1
            if self.send_error is not None:
                raise self.send_error

    def makefile(self, *args):
        return self.file

    def close(self):
        self.closed = True


def exchange(first, replacement=None, *, replay_safe=True, reused=True):
    """Run actual HTTPConnection.request/getresponse/HTTPResponse parsing."""
    replacement = replacement or Socket()
    real_connection = http.client.HTTPConnection
    signer = VaultTransitSigner(
        SigningConfig(provider="vault", vault_addr="http://vault.invalid", council_key_id="test"),
        "test",
    )
    connections = []

    def connection(sock):
        conn = real_connection("vault.invalid")
        conn.sock = sock
        connections.append(conn)
        return conn

    if reused:
        signer._local.connection = connection(first)
    fresh = iter([replacement] if reused else [first, replacement])

    def factory(*args, **kwargs):
        return connection(next(fresh))

    try:
        with patch("http.client.HTTPConnection", factory):
            return signer._send(
                "POST", "v1/transit/sign/test", {}, b"fixed", replay_safe=replay_safe
            )
    finally:
        signer._close_connection()
        assert all(conn.sock is None for conn in connections)


@pytest.mark.parametrize("failure", [ConnectionResetError, ConnectionAbortedError, TimeoutError])
def test_status_received_then_header_failure_is_not_replayed(failure):
    first = Socket(b"HTTP/1.1 200 OK\r\n", header_error=failure("controlled header failure"))
    second = Socket()
    with pytest.raises(failure):
        exchange(first, second)
    assert (first.requests, second.requests) == (1, 0)


def test_interim_response_then_eof_is_not_empty_response():
    first = Socket(b"HTTP/1.1 100 Continue\r\n\r\n")
    second = Socket()
    with pytest.raises(http.client.HTTPException):
        exchange(first, second)
    assert (first.requests, second.requests) == (1, 0)


@pytest.mark.parametrize("wire", [b"HTT", b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nx"])
def test_malformed_status_or_truncated_body_is_not_replayed(wire):
    first, second = Socket(wire), Socket()
    with pytest.raises(http.client.HTTPException):
        exchange(first, second)
    assert (first.requests, second.requests) == (1, 0)


def test_body_reset_is_not_replayed():
    first, second = Socket(body_error=ConnectionResetError("controlled body reset")), Socket()
    with pytest.raises(ConnectionResetError):
        exchange(first, second)
    assert (first.requests, second.requests) == (1, 0)


def test_empty_response_on_reused_connection_retries_once():
    first, second = Socket(b""), Socket()
    assert exchange(first, second) == (200, b"{}")
    assert (first.requests, second.requests) == (1, 1)


@pytest.mark.parametrize(
    "failure",
    [BrokenPipeError, ConnectionResetError, ConnectionAbortedError, http.client.CannotSendRequest],
)
def test_stale_send_on_reused_connection_retries_once(failure):
    first, second = Socket(send_error=failure("controlled stale send")), Socket()
    assert exchange(first, second) == (200, b"{}")
    assert (first.requests, second.requests) == (1, 1)


def test_send_timeout_is_not_replayed():
    first, second = Socket(send_error=TimeoutError("controlled send timeout")), Socket()
    with pytest.raises(TimeoutError):
        exchange(first, second)
    assert (first.requests, second.requests) == (1, 0)


def test_second_empty_response_is_not_retried_again():
    first, second = Socket(b""), Socket(b"")
    with pytest.raises(http.client.RemoteDisconnected):
        exchange(first, second)
    assert (first.requests, second.requests) == (1, 1)


def test_fresh_connection_empty_response_is_not_retried():
    first, second = Socket(b""), Socket()
    with pytest.raises(http.client.RemoteDisconnected):
        exchange(first, second, reused=False)
    assert (first.requests, second.requests) == (1, 0)


def test_non_replay_safe_request_uses_fresh_connection_without_retry():
    stale, fresh = Socket(), Socket(b"")
    with pytest.raises(http.client.RemoteDisconnected):
        exchange(stale, fresh, replay_safe=False)
    assert stale.closed
    assert (stale.requests, fresh.requests) == (0, 1)
