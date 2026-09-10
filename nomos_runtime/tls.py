# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared, fail-closed TLS configuration for Nomos services.

The module deliberately depends only on the Python standard library.  Optional
helpers import httpx/grpc lazily so the lean executor and monitor images can use
the same configuration contract without pulling in the full Nomos package.
"""

from __future__ import annotations

import hashlib
import os
import ssl
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class TLSConfigurationError(ValueError):
    """Raised before network I/O when TLS material is unsafe or incomplete."""


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Return redirect responses to the caller without following them."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _env_bool(value: str | None, *, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise TLSConfigurationError(f"invalid boolean value {value!r}")


def _minimum_version(value: str) -> ssl.TLSVersion:
    normalized = value.strip().lower().replace("_", "").replace(".", "")
    if normalized in {"tlsv12", "tls12", "12"}:
        return ssl.TLSVersion.TLSv1_2
    if normalized in {"tlsv13", "tls13", "13"}:
        return ssl.TLSVersion.TLSv1_3
    raise TLSConfigurationError("minimum TLS version must be TLSv1.2 or TLSv1.3")


def _readable_file(path: str, label: str) -> str:
    candidate = Path(path).expanduser()
    if not candidate.is_file():
        raise TLSConfigurationError(f"{label} is not a readable file: {path!r}")
    try:
        with candidate.open("rb") as handle:
            handle.read(1)
    except OSError as exc:
        raise TLSConfigurationError(f"{label} is not readable: {path!r}") from exc
    return str(candidate)


@dataclass(frozen=True)
class ClientTLSConfig:
    """Trust and identity material for an outbound TLS connection."""

    enabled: bool = False
    ca_file: str = ""
    cert_file: str = ""
    key_file: str = ""
    server_name: str = ""
    verify_hostname: bool = True
    minimum_version: str = "TLSv1.2"

    @classmethod
    def from_env(
        cls,
        prefix: str,
        environ: Mapping[str, str] | None = None,
    ) -> ClientTLSConfig:
        """Load ``<PREFIX>_TLS_*`` variables.

        TLS is explicit: supplying files while ``TLS_ENABLED`` is false is an
        error, preventing a typo from silently turning an intended TLS link
        into plaintext.
        """
        env = os.environ if environ is None else environ
        base = prefix.rstrip("_")
        return cls(
            enabled=_env_bool(env.get(f"{base}_TLS_ENABLED"), default=False),
            ca_file=env.get(f"{base}_TLS_CA_FILE", "").strip(),
            cert_file=env.get(f"{base}_TLS_CERT_FILE", "").strip(),
            key_file=env.get(f"{base}_TLS_KEY_FILE", "").strip(),
            server_name=env.get(f"{base}_TLS_SERVER_NAME", "").strip(),
            verify_hostname=_env_bool(env.get(f"{base}_TLS_VERIFY_HOSTNAME"), default=True),
            minimum_version=env.get(f"{base}_TLS_MINIMUM_VERSION", "TLSv1.2").strip(),
        ).validated()

    def validated(self) -> ClientTLSConfig:
        material = (self.ca_file, self.cert_file, self.key_file, self.server_name)
        if not self.enabled:
            if any(material) or not self.verify_hostname:
                raise TLSConfigurationError(
                    "TLS material or overrides were configured while TLS is disabled"
                )
            return self
        if not self.ca_file:
            raise TLSConfigurationError("TLS client requires a CA bundle")
        if bool(self.cert_file) != bool(self.key_file):
            raise TLSConfigurationError(
                "TLS client certificate and private key must be configured together"
            )
        _readable_file(self.ca_file, "TLS CA bundle")
        if self.cert_file:
            _readable_file(self.cert_file, "TLS client certificate")
            _readable_file(self.key_file, "TLS client private key")
        _minimum_version(self.minimum_version)
        return self

    @property
    def mutual(self) -> bool:
        return self.enabled and bool(self.cert_file)

    def ssl_context(self) -> ssl.SSLContext | bool:
        """Return an httpx/urllib-compatible context, or ``True`` when off."""
        if not self.enabled:
            return True
        self.validated()
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=self.ca_file)
        context.minimum_version = _minimum_version(self.minimum_version)
        context.check_hostname = self.verify_hostname
        context.verify_mode = ssl.CERT_REQUIRED
        if self.cert_file:
            context.load_cert_chain(self.cert_file, self.key_file)
        return context

    def urllib_context(self) -> ssl.SSLContext | None:
        """Return the context shape expected by ``urllib.request.urlopen``."""
        context = self.ssl_context()
        return context if isinstance(context, ssl.SSLContext) else None

    def urllib_kwargs(self) -> dict[str, ssl.SSLContext]:
        """Return kwargs for urlopen without perturbing plaintext test doubles."""
        context = self.urllib_context()
        return {"context": context} if context is not None else {}

    def httpx_kwargs(self) -> dict[str, object]:
        """Return explicit verification settings for httpx clients."""
        return {"verify": self.ssl_context()} if self.enabled else {}

    def mcp_http_client_factory(self) -> Callable[..., Any]:
        """Create an MCP-compatible httpx factory that preserves mTLS settings."""
        context = self.ssl_context()

        def factory(headers=None, timeout=None, auth=None):
            try:
                import httpx
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise TLSConfigurationError("httpx is not installed") from exc
            kwargs: dict[str, object] = {"follow_redirects": True}
            if timeout is not None:
                kwargs["timeout"] = timeout
            if headers is not None:
                kwargs["headers"] = headers
            if auth is not None:
                kwargs["auth"] = auth
            if self.enabled:
                kwargs["verify"] = context
            return httpx.AsyncClient(**kwargs)

        return factory

    def grpc_channel_credentials(self) -> Any:
        """Build ``grpc.ChannelCredentials`` from the same verified material."""
        if not self.enabled:
            raise TLSConfigurationError("gRPC TLS credentials requested while TLS is disabled")
        self.validated()
        try:
            import grpc
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise TLSConfigurationError("grpc is not installed") from exc
        root = Path(self.ca_file).read_bytes()
        cert = Path(self.cert_file).read_bytes() if self.cert_file else None
        key = Path(self.key_file).read_bytes() if self.key_file else None
        return grpc.ssl_channel_credentials(
            root_certificates=root,
            private_key=key,
            certificate_chain=cert,
        )

    def grpc_channel_options(self) -> tuple[tuple[str, str], ...]:
        """Return the explicit SNI/hostname override, when configured."""
        if not self.server_name:
            return ()
        return (("grpc.ssl_target_name_override", self.server_name),)

    def redacted(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "ca_file": self.ca_file,
            "cert_file": self.cert_file,
            "key_file": self.key_file,
            "server_name": self.server_name,
            "verify_hostname": self.verify_hostname,
            "minimum_version": self.minimum_version,
        }


@dataclass(frozen=True)
class ServerTLSConfig:
    """Certificate and client trust material for an inbound TLS listener."""

    enabled: bool = False
    cert_file: str = ""
    key_file: str = ""
    client_ca_file: str = ""
    require_client_cert: bool = True
    minimum_version: str = "TLSv1.2"

    @classmethod
    def from_env(
        cls,
        prefix: str,
        environ: Mapping[str, str] | None = None,
    ) -> ServerTLSConfig:
        env = os.environ if environ is None else environ
        base = prefix.rstrip("_")
        return cls(
            enabled=_env_bool(env.get(f"{base}_TLS_ENABLED"), default=False),
            cert_file=env.get(f"{base}_TLS_CERT_FILE", "").strip(),
            key_file=env.get(f"{base}_TLS_KEY_FILE", "").strip(),
            client_ca_file=env.get(f"{base}_TLS_CLIENT_CA_FILE", "").strip(),
            require_client_cert=_env_bool(env.get(f"{base}_TLS_REQUIRE_CLIENT_CERT"), default=True),
            minimum_version=env.get(f"{base}_TLS_MINIMUM_VERSION", "TLSv1.2").strip(),
        ).validated()

    def validated(self) -> ServerTLSConfig:
        material = (self.cert_file, self.key_file, self.client_ca_file)
        if not self.enabled:
            if any(material) or not self.require_client_cert:
                raise TLSConfigurationError(
                    "TLS listener material or overrides were configured while TLS is disabled"
                )
            return self
        if not self.cert_file or not self.key_file:
            raise TLSConfigurationError("TLS listener requires a certificate and private key")
        if self.require_client_cert and not self.client_ca_file:
            raise TLSConfigurationError("mTLS listener requires a client CA bundle")
        _readable_file(self.cert_file, "TLS server certificate")
        _readable_file(self.key_file, "TLS server private key")
        if self.client_ca_file:
            _readable_file(self.client_ca_file, "TLS client CA bundle")
        _minimum_version(self.minimum_version)
        return self

    def ssl_context(self) -> ssl.SSLContext | None:
        if not self.enabled:
            return None
        self.validated()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = _minimum_version(self.minimum_version)
        context.load_cert_chain(self.cert_file, self.key_file)
        if self.client_ca_file:
            context.load_verify_locations(cafile=self.client_ca_file)
        context.verify_mode = ssl.CERT_REQUIRED if self.require_client_cert else ssl.CERT_NONE
        return context

    def grpc_server_credentials(self) -> Any:
        if not self.enabled:
            raise TLSConfigurationError("gRPC TLS credentials requested while TLS is disabled")
        self.validated()
        try:
            import grpc
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise TLSConfigurationError("grpc is not installed") from exc
        return grpc.ssl_server_credentials(
            [(Path(self.key_file).read_bytes(), Path(self.cert_file).read_bytes())],
            root_certificates=(
                Path(self.client_ca_file).read_bytes() if self.client_ca_file else None
            ),
            require_client_auth=self.require_client_cert,
        )


@dataclass(frozen=True)
class CertificateIdentity:
    """Non-authoritative identity facts extracted from a PEM certificate."""

    sha256_fingerprint: str
    uri_sans: tuple[str, ...]
    dns_sans: tuple[str, ...]


def certificate_identity(path: str) -> CertificateIdentity:
    """Extract URI/DNS SANs and the SHA-256 fingerprint without authorizing it."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
    except ImportError as exc:  # pragma: no cover - package dependency in Nomos
        raise TLSConfigurationError("cryptography is required to inspect certificates") from exc
    data = Path(_readable_file(path, "certificate")).read_bytes()
    try:
        certificate = x509.load_pem_x509_certificate(data)
    except ValueError as exc:
        raise TLSConfigurationError(f"certificate is not valid PEM: {path!r}") from exc
    try:
        sans = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        uris = tuple(sans.get_values_for_type(x509.UniformResourceIdentifier))
        dns = tuple(sans.get_values_for_type(x509.DNSName))
    except x509.ExtensionNotFound:
        uris, dns = (), ()
    return CertificateIdentity(
        sha256_fingerprint=certificate.fingerprint(hashes.SHA256()).hex(),
        uri_sans=uris,
        dns_sans=dns,
    )


def require_secure_url(
    url: str,
    *,
    tls: ClientTLSConfig,
    label: str,
    strict: bool,
) -> None:
    """Reject a TLS/configuration mismatch before credentials can be sent."""
    scheme = urlparse(url).scheme.lower()
    if tls.enabled and scheme != "https":
        raise TLSConfigurationError(f"{label} must use https:// when TLS is enabled")
    if strict and (scheme != "https" or not tls.mutual or not tls.verify_hostname):
        raise TLSConfigurationError(
            f"{label} must use explicitly configured mTLS in this deployment profile"
        )


def require_strict_listener(tls: ServerTLSConfig, *, label: str, strict: bool) -> None:
    """Require mutual client authentication for non-development listeners."""
    if strict and (not tls.enabled or not tls.require_client_cert or not tls.client_ca_file):
        raise TLSConfigurationError(f"{label} must require verified client certificates")


def listener_tls_mode(tls: ServerTLSConfig) -> str:
    """Describe the effective listener transport and client-authentication mode."""
    if not tls.enabled:
        return "plaintext development mode"
    if tls.require_client_cert:
        return "mutual TLS"
    return "one-way TLS"


def urlopen_no_redirect(
    request: urllib.request.Request,
    *,
    timeout: float,
    context: ssl.SSLContext | None = None,
) -> Any:
    """Open a URL while refusing redirects that could leak sensitive headers."""
    handlers: list[Any] = [_RejectRedirects()]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)
    return opener.open(request, timeout=timeout)


def strict_transport_profile(profile: str | None) -> bool:
    return (profile or "dev").strip().lower() in {"alpha", "production", "prod"}


def sha256_file(path: str) -> str:
    """Return a fingerprint for diagnostics without logging file contents."""
    return hashlib.sha256(Path(_readable_file(path, "file")).read_bytes()).hexdigest()
