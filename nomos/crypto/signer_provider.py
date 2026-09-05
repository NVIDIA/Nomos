# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Signer providers: signing without private keys on pod filesystems.

`SignerProvider` is the single signing interface for council writs, ledger
entries, and executor evidence. Implementations:

- InMemorySigner   — wraps an in-memory KeyPair (tests, ephemeral dev)
- LocalFileSigner  — wraps CouncilKeyManager's on-disk key (dev/back-compat)
- VaultTransitSigner — Vault transit engine; private key bytes never leave
  Vault. Auth: kubernetes (projected SA token) or AppRole fallback.

Vault HTTP calls are synchronous by design: signing happens inside
thread-offloaded paths (ledger append transactions, writ minting via
asyncio.to_thread), never directly on the event loop.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol, runtime_checkable

from nomos.config.settings import SigningConfig
from nomos.crypto.key_manager import CouncilKeyManager
from nomos.level_0.crypto.signing import KeyPair, Signature, Signer
from nomos_runtime import ClientTLSConfig, require_secure_url, strict_transport_profile

logger = logging.getLogger(__name__)


@runtime_checkable
class SignerProvider(Protocol):
    """Signing interface that never exposes private key material."""

    @property
    def key_id(self) -> str: ...

    def sign(self, payload: bytes) -> Signature: ...

    def public_key_bytes(self) -> bytes: ...

    def fingerprint(self) -> str: ...


class InMemorySigner:
    """Wraps a KeyPair held in memory. For tests and ephemeral dev use."""

    def __init__(self, key_pair: KeyPair | None = None):
        self._key_pair = key_pair or KeyPair.generate()
        self._signer = Signer(self._key_pair)

    @property
    def key_id(self) -> str:
        return self._key_pair.key_id

    @property
    def key_pair(self) -> KeyPair:
        return self._key_pair

    def sign(self, payload: bytes) -> Signature:
        return self._signer.sign(payload)

    def public_key_bytes(self) -> bytes:
        return self._key_pair.public_key

    def fingerprint(self) -> str:
        return self._key_pair.fingerprint()


class LocalFileSigner:
    """Wraps the existing on-disk CouncilKeyManager. Development back-compat."""

    def __init__(self, key_dir: str):
        self._manager = CouncilKeyManager(key_dir)
        self._manager.load_or_generate()
        self._signer = Signer(self._manager.key_pair)

    @property
    def key_id(self) -> str:
        return self._manager.key_pair.key_id

    @property
    def key_pair(self) -> KeyPair:
        return self._manager.key_pair

    @property
    def key_manager(self) -> CouncilKeyManager:
        return self._manager

    def sign(self, payload: bytes) -> Signature:
        return self._signer.sign(payload)

    def public_key_bytes(self) -> bytes:
        return self._manager.key_pair.public_key

    def fingerprint(self) -> str:
        return self._manager.key_pair.fingerprint()


class VaultSignerError(Exception):
    """Raised when Vault signing or key retrieval fails (callers fail closed)."""


class VaultTransitSigner:
    """Ed25519 signing through the Vault transit engine.

    The private key lives in Vault; this signer only ever sees signatures and
    the public key. The client token is obtained via kubernetes auth (projected
    service-account JWT) when a role is configured, else AppRole. A 403 on any
    call triggers one re-login + retry (tokens expire).
    """

    def __init__(self, config: SigningConfig, key_id: str):
        if not key_id:
            raise ValueError("VaultTransitSigner requires a key id")
        if not config.vault_addr:
            raise ValueError("VaultTransitSigner requires vault_addr (NOMOS_VAULT_ADDR)")
        self._config = config
        self._key_id = key_id
        self._token: str | None = None
        self._public_key: bytes | None = None
        self._tls = ClientTLSConfig.from_env("NOMOS_VAULT")
        require_secure_url(
            config.vault_addr,
            tls=self._tls,
            label="NOMOS_VAULT_ADDR",
            strict=strict_transport_profile(os.environ.get("NOMOS_DEPLOY_PROFILE")),
        )

    @property
    def key_id(self) -> str:
        return self._key_id

    def sign(self, payload: bytes) -> Signature:
        encoded = base64.b64encode(payload).decode()
        result = self._request_with_reauth(
            "POST",
            f"v1/{self._config.vault_transit_mount}/sign/{self._key_id}",
            {"input": encoded},
        )
        raw = result.get("data", {}).get("signature", "")
        # Format: "vault:v<keyversion>:<base64 signature>"
        try:
            sig_b64 = raw.rsplit(":", 1)[1]
            sig_bytes = base64.b64decode(sig_b64)
        except (IndexError, ValueError) as exc:
            raise VaultSignerError(f"unexpected vault signature format: {raw!r}") from exc
        return Signature(signer_id=self._key_id, signature_bytes=sig_bytes)

    def public_key_bytes(self) -> bytes:
        if self._public_key is None:
            result = self._request_with_reauth(
                "GET", f"v1/{self._config.vault_transit_mount}/keys/{self._key_id}", None
            )
            keys = result.get("data", {}).get("keys", {})
            latest = str(result.get("data", {}).get("latest_version", "")) or (
                max(keys, key=int) if keys else ""
            )
            entry = keys.get(latest) or {}
            public_b64 = entry.get("public_key", "")
            if not public_b64:
                raise VaultSignerError(f"vault key {self._key_id} has no public_key")
            self._public_key = base64.b64decode(public_b64)
        return self._public_key

    def fingerprint(self) -> str:
        return hashlib.sha256(self.public_key_bytes()).hexdigest()

    # ── internals ──────────────────────────────────────────────────────

    def _login(self) -> str:
        cfg = self._config
        if cfg.vault_k8s_role:
            jwt = Path(cfg.vault_k8s_token_path).read_text().strip()
            path, body = "v1/auth/kubernetes/login", {"role": cfg.vault_k8s_role, "jwt": jwt}
        elif cfg.vault_role_id:
            path, body = (
                "v1/auth/approle/login",
                {"role_id": cfg.vault_role_id, "secret_id": cfg.vault_secret_id},
            )
        else:
            raise VaultSignerError(
                "no Vault auth configured: set NOMOS_VAULT_K8S_ROLE or VAULT_ROLE_ID"
            )
        result = self._request("POST", path, body, token=None)
        token = str(result.get("auth", {}).get("client_token", ""))
        if not token:
            raise VaultSignerError("vault login returned no client_token")
        return token

    def _request_with_reauth(self, method: str, path: str, body: dict | None) -> dict:
        if self._token is None:
            self._token = self._login()
        try:
            return self._request(method, path, body, token=self._token)
        except VaultSignerError as exc:
            if "403" not in str(exc):
                raise
            logger.info("vault token rejected; re-authenticating")
            self._token = self._login()
            return self._request(method, path, body, token=self._token)

    def _request(self, method: str, path: str, body: dict | None, *, token: str | None) -> dict:
        addr = self._config.vault_addr.rstrip("/")
        if not addr.startswith(("https://", "http://")):
            raise VaultSignerError(f"vault_addr must be http(s), got {addr!r}")
        url = f"{addr}/{path}"
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Vault-Token"] = token
        if self._config.vault_namespace:
            headers["X-Vault-Namespace"] = self._config.vault_namespace
        request = urllib.request.Request(  # noqa: S310 - scheme validated above
            url,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(  # type: ignore[call-overload]  # noqa: S310
                request,
                timeout=self._config.timeout_s,
                **self._tls.urllib_kwargs(),
            ) as response:
                result: dict = json.loads(response.read() or b"{}")
                return result
        except urllib.error.HTTPError as exc:
            raise VaultSignerError(f"vault {method} {path} failed: {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise VaultSignerError(f"vault {method} {path} failed: {exc}") from exc


def create_signer(
    config: SigningConfig,
    *,
    key_id: str,
    local_key_dir: str = "",
) -> SignerProvider:
    """Build the configured signer.

    provider=vault → VaultTransitSigner(key_id). provider=local → on-disk
    LocalFileSigner when a key directory is configured, else an in-memory
    signer (ephemeral; fine for tests, wrong for multi-replica production).
    """
    if config.provider == "vault":
        return VaultTransitSigner(config, key_id)
    if local_key_dir:
        return LocalFileSigner(local_key_dir)
    logger.warning(
        "signing.provider=local with no key directory — using an ephemeral in-memory "
        "key; signatures will not verify across restarts or replicas"
    )
    return InMemorySigner()
