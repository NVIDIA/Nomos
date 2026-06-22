# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


"""
Tests for key rotation support — Phase 6.3.

Verifies that the executor can accept writs signed by either
the current or previous council key during a rotation window.
"""

import base64
import hashlib
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from executors.docker.config import ExecutorConfig
from executors.docker.enforcer import WritEnforcer
from executors.docker.nonce_store import NonceStore
from nomos.level_0.crypto.signing import KeyPair
from nomos.level_0.types.writ import WritOfExecution as NomosWrit


@pytest.fixture
def mock_nonce_store():
    store = MagicMock(spec=NonceStore)
    store.claim_nonce = AsyncMock(return_value=True)
    store.release_nonce = AsyncMock()
    store.report_outcome = AsyncMock()
    return store


@pytest.fixture
def config():
    return ExecutorConfig(allowed_working_directories=["/tmp"])


def _mint_and_serialize(kp: KeyPair, nonce: str = "aa" * 32) -> str:
    """Mint a signed writ and serialize to base64 blob."""
    now = time.time()
    writ = NomosWrit(
        writ_id="writ-rotation-test",
        intent_hash="bb" * 32,
        command="echo rotation-test",
        working_directory="/tmp",
        target_agent_id="agent-test",
        issued_at=now - 5,
        expires_at=now + 300,
        max_executions=1,
        nonce=nonce,
        council_id="council-test",
        threshold_used="quorum",
        chairman_risk_level="low",
    )
    signed = writ.sign(kp.private_key)
    return base64.b64encode(signed.model_dump_json().encode()).decode()


class TestKeyRotation:
    @pytest.mark.asyncio
    async def test_current_key_works(self, mock_nonce_store, config):
        """Writ signed with current key verifies."""
        current_kp = KeyPair.generate(key_id="current")
        enforcer = WritEnforcer(
            public_key=current_kp.public_key,
            nonce_store=mock_nonce_store,
            config=config,
        )

        blob = _mint_and_serialize(current_kp)
        result = await enforcer.execute_writ(blob)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_old_key_accepted_during_rotation(self, mock_nonce_store, config):
        """Writ signed with old key still verifies during rotation window."""
        old_kp = KeyPair.generate(key_id="old-key")
        new_kp = KeyPair.generate(key_id="new-key")

        old_fingerprint = hashlib.sha256(old_kp.public_key).hexdigest()
        enforcer = WritEnforcer(
            public_key=new_kp.public_key,
            nonce_store=mock_nonce_store,
            config=config,
            additional_keys={old_fingerprint: old_kp.public_key},
        )

        # Writ signed with OLD key
        blob = _mint_and_serialize(old_kp)
        result = await enforcer.execute_writ(blob)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_new_key_works_during_rotation(self, mock_nonce_store, config):
        """Writ signed with new key also works during rotation."""
        old_kp = KeyPair.generate(key_id="old-key")
        new_kp = KeyPair.generate(key_id="new-key")

        old_fingerprint = hashlib.sha256(old_kp.public_key).hexdigest()
        enforcer = WritEnforcer(
            public_key=new_kp.public_key,
            nonce_store=mock_nonce_store,
            config=config,
            additional_keys={old_fingerprint: old_kp.public_key},
        )

        blob = _mint_and_serialize(new_kp, nonce="dd" * 32)
        result = await enforcer.execute_writ(blob)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_unknown_key_rejected(self, mock_nonce_store, config):
        """Writ signed with unknown key is rejected even during rotation."""
        old_kp = KeyPair.generate(key_id="old")
        new_kp = KeyPair.generate(key_id="new")
        rogue_kp = KeyPair.generate(key_id="rogue")

        old_fp = hashlib.sha256(old_kp.public_key).hexdigest()
        enforcer = WritEnforcer(
            public_key=new_kp.public_key,
            nonce_store=mock_nonce_store,
            config=config,
            additional_keys={old_fp: old_kp.public_key},
        )

        blob = _mint_and_serialize(rogue_kp)
        result = await enforcer.execute_writ(blob)
        assert result.success is False
        assert result.verification_stage == "signature"

    @pytest.mark.asyncio
    async def test_single_key_mode(self, mock_nonce_store, config):
        """With no additional_keys, only the primary key is accepted."""
        kp = KeyPair.generate()
        enforcer = WritEnforcer(
            public_key=kp.public_key,
            nonce_store=mock_nonce_store,
            config=config,
        )
        assert len(enforcer._keys) == 1

        blob = _mint_and_serialize(kp)
        result = await enforcer.execute_writ(blob)
        assert result.success is True
