"""Executor signing-identity selection: Vault transit signer or local key files.

Mirrors the council's signer selection (nomos.nat.functions._init_signer): when
NOMOS_SIGNER_PROVIDER=vault the executor signs evidence through the Vault transit
engine (private key bytes never reach the process), otherwise it keeps the
historical on-disk Ed25519 key behavior.
"""

from __future__ import annotations

import logging
import os

from executors.common import executor_identity_from_signer, load_or_create_executor_identity

logger = logging.getLogger(__name__)


def build_executor_identity(
    *,
    private_key_path: str = "",
    public_key_path: str = "",
    signing=None,
):
    """Return the executor evidence identity for the current configuration.

    Vault mode builds a VaultTransitSigner from the signing config and
    EXECUTOR_SIGNER_KEY_ID; local mode loads/creates the on-disk key. `signing`
    is injectable for tests; otherwise it is read from the environment via the
    shared SigningConfig.
    """
    if signing is None:
        from nomos.config.settings import SigningConfig

        signing = SigningConfig()

    if signing.provider == "vault":
        from nomos.crypto.signer_provider import VaultTransitSigner

        key_id = os.environ.get("EXECUTOR_SIGNER_KEY_ID", "")
        signer = VaultTransitSigner(signing, key_id)
        logger.info("Executor evidence: Vault transit signer (key=%s)", key_id)
        return executor_identity_from_signer(signer)

    return load_or_create_executor_identity(private_key_path, public_key_path)
