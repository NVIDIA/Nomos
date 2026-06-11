# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Executor signing-identity selection: Vault transit signer or local key files.

Mirrors the council's signer selection: when NOMOS_SIGNER_PROVIDER=vault the
executor signs evidence through the Vault transit engine (private key bytes
never reach the process), otherwise it keeps the historical on-disk Ed25519 key.

NOMOS-FREE BY DESIGN: the executor image ships ``nomos_runtime`` but not the
``nomos`` package (deploy/Dockerfile.executor COPYs only trusted_executor_contract,
nomos_runtime, executors). Importing ``nomos.*`` here would raise
``ModuleNotFoundError`` at runtime inside the executor container, so the Vault
signer is the self-contained ``executors.common.vault_signer``.
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

    Vault mode builds a stdlib ``ExecutorVaultTransitSigner`` from
    ``EXECUTOR_SIGNER_KEY_ID`` and the ``NOMOS_VAULT_*`` env (or an injected
    ``signing`` config in tests); local mode loads/creates the on-disk key.
    ``signing`` is read by attribute only — never imported from ``nomos``.
    """
    provider = (
        (
            signing.provider
            if signing is not None
            else os.environ.get("NOMOS_SIGNER_PROVIDER", "local")
        )
        .strip()
        .lower()
    )

    if provider == "vault":
        from executors.common.vault_signer import ExecutorVaultTransitSigner

        key_id = os.environ.get("EXECUTOR_SIGNER_KEY_ID", "")
        signer = (
            ExecutorVaultTransitSigner.from_signing(key_id, signing)
            if signing is not None
            else ExecutorVaultTransitSigner.from_env(key_id)
        )
        logger.info("Executor evidence: Vault transit signer (key=%s)", key_id)
        return executor_identity_from_signer(signer)

    return load_or_create_executor_identity(private_key_path, public_key_path)
