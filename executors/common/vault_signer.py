"""Vault transit signer for trusted executors (nomos-free).

The executor image ships ``nomos_runtime`` but not the ``nomos`` package, so
the executor cannot import ``nomos.crypto.signer_provider.VaultTransitSigner``.
This is a self-contained port that speaks the same Vault transit HTTP contract
(``POST v1/<mount>/sign/<key>``, ``GET v1/<mount>/keys/<key>``, kubernetes /
AppRole auth) using only the standard library, so executor evidence can be
signed by Vault transit with the private key never reaching the pod filesystem.

Config is read from the same deployment env contract the council uses
(``NOMOS_VAULT_*``); the executor supplies its own ``EXECUTOR_SIGNER_KEY_ID``.
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
from typing import NamedTuple

logger = logging.getLogger(__name__)

DEFAULT_TRANSIT_MOUNT = "transit"
DEFAULT_K8S_TOKEN_PATH = "/var/run/secrets/nomos/vault-token"  # noqa: S105  # nosec B105 (path)
DEFAULT_TIMEOUT_S = 5.0


class ExecutorVaultSignerError(Exception):
    """Raised when Vault signing or key retrieval fails (callers fail closed)."""


class _Signature(NamedTuple):
    """Matches the ``.signature_bytes`` surface ProviderExecutorIdentity expects."""

    signer_id: str
    signature_bytes: bytes


class ExecutorVaultTransitSigner:
    """Ed25519 signing through the Vault transit engine, stdlib-only.

    Mirrors ``nomos.crypto.signer_provider.VaultTransitSigner`` so a single
    in-cluster Vault transit mount serves both council and executor. The client
    token is obtained via kubernetes auth (projected service-account JWT) when a
    role is set, else AppRole. A 403 on any call triggers one re-login + retry.
    """

    def __init__(
        self,
        *,
        key_id: str,
        vault_addr: str,
        transit_mount: str = DEFAULT_TRANSIT_MOUNT,
        namespace: str = "",
        k8s_role: str = "",
        k8s_token_path: str = DEFAULT_K8S_TOKEN_PATH,
        role_id: str = "",
        secret_id: str = "",
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        if not key_id:
            raise ValueError("ExecutorVaultTransitSigner requires a key id")
        if not vault_addr:
            raise ValueError("ExecutorVaultTransitSigner requires vault_addr (NOMOS_VAULT_ADDR)")
        self._key_id = key_id
        self._vault_addr = vault_addr.rstrip("/")
        self._transit_mount = transit_mount or DEFAULT_TRANSIT_MOUNT
        self._namespace = namespace
        self._k8s_role = k8s_role
        self._k8s_token_path = k8s_token_path or DEFAULT_K8S_TOKEN_PATH
        self._role_id = role_id
        self._secret_id = secret_id
        self._timeout_s = timeout_s
        self._token: str | None = None
        self._public_key: bytes | None = None

    @classmethod
    def from_env(cls, key_id: str) -> ExecutorVaultTransitSigner:
        """Build the signer from the NOMOS_VAULT_* deployment env contract."""
        try:
            timeout_s = float(os.environ.get("NOMOS_VAULT_TIMEOUT_S", "") or DEFAULT_TIMEOUT_S)
        except ValueError:
            timeout_s = DEFAULT_TIMEOUT_S
        return cls(
            key_id=key_id,
            vault_addr=os.environ.get("NOMOS_VAULT_ADDR", ""),
            transit_mount=os.environ.get("NOMOS_VAULT_TRANSIT_MOUNT", "") or DEFAULT_TRANSIT_MOUNT,
            namespace=os.environ.get("NOMOS_VAULT_NAMESPACE", ""),
            k8s_role=os.environ.get("NOMOS_VAULT_K8S_ROLE", ""),
            k8s_token_path=os.environ.get("NOMOS_VAULT_K8S_TOKEN_PATH", "")
            or DEFAULT_K8S_TOKEN_PATH,
            role_id=os.environ.get("VAULT_ROLE_ID", ""),
            secret_id=os.environ.get("VAULT_SECRET_ID", ""),
            timeout_s=timeout_s,
        )

    @classmethod
    def from_signing(cls, key_id: str, signing: object) -> ExecutorVaultTransitSigner:
        """Build from a duck-typed signing config (council ``SigningConfig`` attrs).

        Accepts any object exposing the ``vault_*`` attribute names; used for
        dependency injection in tests. Production reads the env via ``from_env``.
        Never imports ``nomos`` — the object is read by attribute only.
        """
        try:
            timeout_s = float(getattr(signing, "timeout_s", DEFAULT_TIMEOUT_S))
        except (TypeError, ValueError):
            timeout_s = DEFAULT_TIMEOUT_S
        return cls(
            key_id=key_id,
            vault_addr=getattr(signing, "vault_addr", ""),
            transit_mount=getattr(signing, "vault_transit_mount", "") or DEFAULT_TRANSIT_MOUNT,
            namespace=getattr(signing, "vault_namespace", ""),
            k8s_role=getattr(signing, "vault_k8s_role", ""),
            k8s_token_path=getattr(signing, "vault_k8s_token_path", "") or DEFAULT_K8S_TOKEN_PATH,
            role_id=getattr(signing, "vault_role_id", ""),
            secret_id=getattr(signing, "vault_secret_id", ""),
            timeout_s=timeout_s,
        )

    @property
    def key_id(self) -> str:
        return self._key_id

    def sign(self, payload: bytes) -> _Signature:
        encoded = base64.b64encode(payload).decode()
        result = self._request_with_reauth(
            "POST",
            f"v1/{self._transit_mount}/sign/{self._key_id}",
            {"input": encoded},
        )
        raw = result.get("data", {}).get("signature", "")
        # Format: "vault:v<keyversion>:<base64 signature>"
        try:
            sig_b64 = raw.rsplit(":", 1)[1]
            sig_bytes = base64.b64decode(sig_b64)
        except (IndexError, ValueError) as exc:
            raise ExecutorVaultSignerError(f"unexpected vault signature format: {raw!r}") from exc
        return _Signature(signer_id=self._key_id, signature_bytes=sig_bytes)

    def public_key_bytes(self) -> bytes:
        if self._public_key is None:
            result = self._request_with_reauth(
                "GET", f"v1/{self._transit_mount}/keys/{self._key_id}", None
            )
            keys = result.get("data", {}).get("keys", {})
            latest = str(result.get("data", {}).get("latest_version", "")) or (
                max(keys, key=int) if keys else ""
            )
            entry = keys.get(latest) or {}
            public_b64 = entry.get("public_key", "")
            if not public_b64:
                raise ExecutorVaultSignerError(f"vault key {self._key_id} has no public_key")
            self._public_key = base64.b64decode(public_b64)
        return self._public_key

    def fingerprint(self) -> str:
        return hashlib.sha256(self.public_key_bytes()).hexdigest()

    # ── internals ──────────────────────────────────────────────────────

    def _login(self) -> str:
        if self._k8s_role:
            jwt = Path(self._k8s_token_path).read_text().strip()
            path, body = "v1/auth/kubernetes/login", {"role": self._k8s_role, "jwt": jwt}
        elif self._role_id:
            path, body = (
                "v1/auth/approle/login",
                {"role_id": self._role_id, "secret_id": self._secret_id},
            )
        else:
            raise ExecutorVaultSignerError(
                "no Vault auth configured: set NOMOS_VAULT_K8S_ROLE or VAULT_ROLE_ID"
            )
        result = self._request("POST", path, body, token=None)
        token = str(result.get("auth", {}).get("client_token", ""))
        if not token:
            raise ExecutorVaultSignerError("vault login returned no client_token")
        return token

    def _request_with_reauth(self, method: str, path: str, body: dict | None) -> dict:
        if self._token is None:
            self._token = self._login()
        try:
            return self._request(method, path, body, token=self._token)
        except ExecutorVaultSignerError as exc:
            if "403" not in str(exc):
                raise
            logger.info("vault token rejected; re-authenticating")
            self._token = self._login()
            return self._request(method, path, body, token=self._token)

    def _request(self, method: str, path: str, body: dict | None, *, token: str | None) -> dict:
        if not self._vault_addr.startswith(("https://", "http://")):
            raise ExecutorVaultSignerError(f"vault_addr must be http(s), got {self._vault_addr!r}")
        url = f"{self._vault_addr}/{path}"
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Vault-Token"] = token
        if self._namespace:
            headers["X-Vault-Namespace"] = self._namespace
        request = urllib.request.Request(  # noqa: S310 - scheme validated above
            url,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - scheme validated above
                request, timeout=self._timeout_s
            ) as response:
                result: dict = json.loads(response.read() or b"{}")
                return result
        except urllib.error.HTTPError as exc:
            raise ExecutorVaultSignerError(f"vault {method} {path} failed: {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ExecutorVaultSignerError(f"vault {method} {path} failed: {exc}") from exc
