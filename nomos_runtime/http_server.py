# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Threaded HTTP listener with worker-boundary TLS handshakes."""

from __future__ import annotations

import logging
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

logger = logging.getLogger(__name__)


class ThreadedTLSHTTPServer(ThreadingHTTPServer):
    """Keep the listening socket plaintext and perform TLS handshakes in workers."""

    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler_class: type[BaseHTTPRequestHandler],
        *,
        bind_and_activate: bool = True,
    ) -> None:
        self._tls_context: ssl.SSLContext | None = None
        self._tls_handshake_timeout = 5.0
        super().__init__(server_address, request_handler_class, bind_and_activate)

    def configure_tls(
        self,
        context: ssl.SSLContext | None,
        *,
        handshake_timeout: float = 5.0,
    ) -> None:
        """Configure TLS for accepted connections without wrapping the listener."""
        self._tls_context = context
        self._tls_handshake_timeout = handshake_timeout

    def finish_request(self, request: Any, client_address: Any) -> None:
        connection = request
        if self._tls_context is not None:
            try:
                connection.settimeout(self._tls_handshake_timeout)
                connection = self._tls_context.wrap_socket(connection, server_side=True)
                connection.settimeout(None)
            except OSError as exc:
                logger.debug("TLS client handshake failed from %s: %s", client_address, exc)
                connection.close()
                return
        super().finish_request(connection, client_address)
