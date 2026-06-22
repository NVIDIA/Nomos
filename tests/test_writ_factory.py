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
Tests for WritFactory — Phase 3.1.

Tests minting, signing, serialization, and risk-level TTL mapping.
"""

import base64

import pytest

from nomos.auth.writ_factory import WritFactory, _compute_intent_hash
from nomos.level_0.crypto.signing import KeyPair
from nomos.level_0.types import Intent, PackagedFile
from nomos.level_0.types.writ import VoteSummary


def _make_intent(command: str = "echo hello") -> Intent:
    return Intent.create(
        agent_id="agent-dev",
        action_type="system.info",
        description="Test command for writ factory",
        target_resource="/tmp",
        justification="Testing",
        command=command,
        parameters={"command": command},
    )


def _make_packaged_file() -> PackagedFile:
    return PackagedFile(
        path="scripts/task.sh",
        content_base64=base64.b64encode(b"#!/bin/sh\necho packaged\n").decode(),
        mode="0755",
        description="Test packaged shell script",
    )


def _make_factory(key_pair: KeyPair = None) -> tuple:
    """Returns (factory, key_pair)."""
    kp = key_pair or KeyPair.generate(key_id="council-test")
    factory = WritFactory(council_key_pair=kp, council_id="council-test")
    return factory, kp


class TestMintWrit:
    @pytest.mark.asyncio
    async def test_minted_writ_verifies(self):
        """Minted writ signature verifies against council public key."""
        factory, kp = _make_factory()
        intent = _make_intent()

        writ = await factory.mint_writ(
            intent=intent,
            target_agent_id="agent-dev",
            threshold_used="quorum",
            member_votes=[VoteSummary(member_id="m1", decision="approve")],
            chairman_risk_level="low",
        )

        assert writ.signature != ""
        assert writ.verify(kp.public_key) is True

    @pytest.mark.asyncio
    async def test_writ_has_correct_command(self):
        factory, _kp = _make_factory()
        intent = _make_intent("ls -la /tmp")

        writ = await factory.mint_writ(
            intent=intent,
            target_agent_id="agent-dev",
            threshold_used="quorum",
            member_votes=[],
            chairman_risk_level="low",
        )

        assert writ.command == "ls -la /tmp"

    @pytest.mark.asyncio
    async def test_writ_id_unique(self):
        factory, _ = _make_factory()
        intent = _make_intent()

        writ1 = await factory.mint_writ(
            intent=intent,
            target_agent_id="a",
            threshold_used="quorum",
            member_votes=[],
            chairman_risk_level="low",
        )
        writ2 = await factory.mint_writ(
            intent=intent,
            target_agent_id="a",
            threshold_used="quorum",
            member_votes=[],
            chairman_risk_level="low",
        )

        assert writ1.writ_id != writ2.writ_id

    @pytest.mark.asyncio
    async def test_nonce_unique(self):
        factory, _ = _make_factory()
        intent = _make_intent()

        writ1 = await factory.mint_writ(
            intent=intent,
            target_agent_id="a",
            threshold_used="quorum",
            member_votes=[],
            chairman_risk_level="low",
        )
        writ2 = await factory.mint_writ(
            intent=intent,
            target_agent_id="a",
            threshold_used="quorum",
            member_votes=[],
            chairman_risk_level="low",
        )

        assert writ1.nonce != writ2.nonce

    @pytest.mark.asyncio
    async def test_intent_hash_matches(self):
        """Intent hash in writ matches independent computation."""
        factory, _ = _make_factory()
        intent = _make_intent()

        writ = await factory.mint_writ(
            intent=intent,
            target_agent_id="a",
            threshold_used="quorum",
            member_votes=[],
            chairman_risk_level="low",
        )

        expected_hash = _compute_intent_hash(intent)
        assert writ.intent_hash == expected_hash

    @pytest.mark.asyncio
    async def test_writ_carries_packaged_file(self):
        factory, kp = _make_factory()
        intent = _make_intent("/bin/sh scripts/task.sh")
        intent.packaged_files.append(_make_packaged_file())

        writ = await factory.mint_writ(
            intent=intent,
            target_agent_id="agent-dev",
            threshold_used="quorum",
            member_votes=[],
            chairman_risk_level="low",
        )

        assert len(writ.packaged_files) == 1
        assert writ.packaged_files[0].sha256 == intent.packaged_files[0].sha256
        assert writ.verify(kp.public_key) is True


class TestRiskLevelTTL:
    @pytest.mark.asyncio
    async def test_low_risk_300s(self):
        factory, _ = _make_factory()
        writ = await factory.mint_writ(
            intent=_make_intent(),
            target_agent_id="a",
            threshold_used="quorum",
            member_votes=[],
            chairman_risk_level="low",
        )
        ttl = writ.expires_at - writ.issued_at
        assert abs(ttl - 300) < 1

    @pytest.mark.asyncio
    async def test_medium_risk_120s(self):
        factory, _ = _make_factory()
        writ = await factory.mint_writ(
            intent=_make_intent(),
            target_agent_id="a",
            threshold_used="quorum",
            member_votes=[],
            chairman_risk_level="medium",
        )
        ttl = writ.expires_at - writ.issued_at
        assert abs(ttl - 120) < 1

    @pytest.mark.asyncio
    async def test_high_risk_60s(self):
        factory, _ = _make_factory()
        writ = await factory.mint_writ(
            intent=_make_intent(),
            target_agent_id="a",
            threshold_used="quorum",
            member_votes=[],
            chairman_risk_level="high",
        )
        ttl = writ.expires_at - writ.issued_at
        assert abs(ttl - 60) < 1

    @pytest.mark.asyncio
    async def test_critical_risk_30s(self):
        factory, _ = _make_factory()
        writ = await factory.mint_writ(
            intent=_make_intent(),
            target_agent_id="a",
            threshold_used="quorum",
            member_votes=[],
            chairman_risk_level="critical",
        )
        ttl = writ.expires_at - writ.issued_at
        assert abs(ttl - 30) < 1


class TestSerialization:
    @pytest.mark.asyncio
    async def test_serialize_deserialize_roundtrip(self):
        """serialize → deserialize → verify"""
        factory, kp = _make_factory()
        writ = await factory.mint_writ(
            intent=_make_intent(),
            target_agent_id="a",
            threshold_used="quorum",
            member_votes=[VoteSummary(member_id="m1", decision="approve")],
            chairman_risk_level="low",
        )

        blob = WritFactory.serialize_writ(writ)
        restored = WritFactory.deserialize_writ(blob)

        assert restored.writ_id == writ.writ_id
        assert restored.command == writ.command
        assert restored.nonce == writ.nonce
        assert restored.packaged_files == writ.packaged_files
        assert restored.signature == writ.signature
        assert restored.verify(kp.public_key) is True

    @pytest.mark.asyncio
    async def test_member_votes_stripped_from_blob(self):
        """member_votes must not appear in the serialized blob (not signed)."""
        import base64
        import json

        factory, kp = _make_factory()
        writ = await factory.mint_writ(
            intent=_make_intent(),
            target_agent_id="a",
            threshold_used="quorum",
            member_votes=[
                VoteSummary(member_id="m1", decision="approve"),
                VoteSummary(member_id="m2", decision="deny", risk_concerns=["dangerous"]),
            ],
            chairman_risk_level="low",
        )
        # Votes exist on the writ object
        assert len(writ.member_votes) == 2

        blob = WritFactory.serialize_writ(writ)
        decoded = json.loads(base64.b64decode(blob))

        # But NOT in the serialized blob
        assert "member_votes" not in decoded

        # Deserialized writ still verifies (votes default to empty list)
        restored = WritFactory.deserialize_writ(blob)
        assert restored.member_votes == []
        assert restored.verify(kp.public_key) is True

    @pytest.mark.asyncio
    async def test_serialized_blob_is_base64(self):
        import base64

        factory, _ = _make_factory()
        writ = await factory.mint_writ(
            intent=_make_intent(),
            target_agent_id="a",
            threshold_used="quorum",
            member_votes=[],
            chairman_risk_level="low",
        )

        blob = WritFactory.serialize_writ(writ)
        # Should be valid base64
        decoded = base64.b64decode(blob)
        assert len(decoded) > 0


class TestIntentHash:
    def test_deterministic(self):
        intent = _make_intent()
        h1 = _compute_intent_hash(intent)
        h2 = _compute_intent_hash(intent)
        assert h1 == h2

    def test_different_commands_different_hash(self):
        i1 = _make_intent("echo 1")
        i2 = _make_intent("echo 2")
        assert _compute_intent_hash(i1) != _compute_intent_hash(i2)
