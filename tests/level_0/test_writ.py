"""
Tests for WritOfExecution data model — Phase 1.1.

Covers canonical serialization, signing, verification, expiration,
and tamper detection.
"""

import base64
import time

from nomos.level_0.crypto.signing import KeyPair
from nomos.level_0.types import PackagedFile, VoteSummary, WritOfExecution


def _make_writ(**overrides) -> WritOfExecution:
    """Create a minimal valid writ for testing."""
    defaults = {
        "writ_id": "writ-test-001",
        "intent_hash": "abcdef1234567890" * 4,
        "command": "echo hello",
        "working_directory": "/app/workspace",
        "target_agent_id": "agent-admin",
        "issued_at": 1700000000.0,
        "expires_at": 1700000300.0,
        "max_executions": 1,
        "nonce": "deadbeef" * 8,
        "council_id": "council-prod",
        "threshold_used": "quorum",
        "chairman_risk_level": "low",
        "ledger_entry_hash": "fedcba9876543210" * 4,
    }
    defaults.update(overrides)
    return WritOfExecution(**defaults)


class TestCanonicalBytes:
    def test_deterministic(self):
        """Same writ produces same canonical bytes on multiple calls."""
        writ = _make_writ()
        assert writ.canonical_bytes() == writ.canonical_bytes()

    def test_deterministic_across_instances(self):
        """Two writs with same fields produce same canonical bytes."""
        writ1 = _make_writ()
        writ2 = _make_writ()
        assert writ1.canonical_bytes() == writ2.canonical_bytes()

    def test_excludes_member_votes(self):
        """member_votes are NOT included in canonical bytes."""
        writ_no_votes = _make_writ(member_votes=[])
        writ_with_votes = _make_writ(
            member_votes=[
                VoteSummary(member_id="m1", decision="approve", risk_concerns=["none"]),
                VoteSummary(member_id="m2", decision="deny", risk_concerns=["dangerous"]),
            ]
        )
        assert writ_no_votes.canonical_bytes() == writ_with_votes.canonical_bytes()

    def test_excludes_signature(self):
        """signature is NOT included in canonical bytes."""
        writ_unsigned = _make_writ(signature="")
        writ_signed = _make_writ(signature="aabb" * 32)
        assert writ_unsigned.canonical_bytes() == writ_signed.canonical_bytes()

    def test_sorted_keys_in_json(self):
        """Canonical JSON has alphabetically sorted keys."""
        import json

        writ = _make_writ()
        data = json.loads(writ.canonical_bytes())
        keys = list(data.keys())
        assert keys == sorted(keys)

    def test_no_env_in_canonical_bytes(self):
        """canonical bytes contain no environment variables (removed field)."""
        import json

        writ = _make_writ()
        data = json.loads(writ.canonical_bytes())
        assert "allowed_env" not in data

    def test_compact_json_separators(self):
        """Canonical bytes use compact JSON separators (no formatting whitespace)."""
        writ = _make_writ(
            command="ls",
            working_directory="/tmp",
            target_agent_id="a",
            council_id="c",
            nonce="aa" * 32,
            intent_hash="bb" * 32,
            ledger_entry_hash="cc" * 32,
        )
        text = writ.canonical_bytes().decode("utf-8")
        # Compact JSON: no spaces after colons or commas in structure
        assert ": " not in text
        assert ", " not in text


class TestSignAndVerify:
    def test_sign_verify_roundtrip(self):
        """Sign canonical bytes → construct writ → verify."""
        kp = KeyPair.generate(key_id="council-test")
        writ = _make_writ()
        signed = writ.sign(kp.private_key)

        assert signed.signature != ""
        assert signed.verify(kp.public_key) is True

    def test_unsigned_writ_fails_verify(self):
        """Writ with no signature fails verification."""
        kp = KeyPair.generate()
        writ = _make_writ(signature="")
        assert writ.verify(kp.public_key) is False

    def test_tampered_command_fails(self):
        """Modifying command after signing invalidates signature."""
        kp = KeyPair.generate()
        signed = _make_writ().sign(kp.private_key)

        tampered = signed.model_copy(update={"command": "rm -rf /"})
        assert tampered.verify(kp.public_key) is False

    def test_tampered_working_directory_fails(self):
        kp = KeyPair.generate()
        signed = _make_writ().sign(kp.private_key)

        tampered = signed.model_copy(update={"working_directory": "/etc"})
        assert tampered.verify(kp.public_key) is False

    def test_tampered_nonce_fails(self):
        kp = KeyPair.generate()
        signed = _make_writ().sign(kp.private_key)

        tampered = signed.model_copy(update={"nonce": "00" * 32})
        assert tampered.verify(kp.public_key) is False

    def test_tampered_expires_at_fails(self):
        kp = KeyPair.generate()
        signed = _make_writ().sign(kp.private_key)

        tampered = signed.model_copy(update={"expires_at": 9999999999.0})
        assert tampered.verify(kp.public_key) is False

    def test_tampered_target_agent_fails(self):
        kp = KeyPair.generate()
        signed = _make_writ().sign(kp.private_key)

        tampered = signed.model_copy(update={"target_agent_id": "attacker"})
        assert tampered.verify(kp.public_key) is False

    def test_tampered_working_directory_invalidates_signature(self):
        """Modifying working_directory after signing invalidates signature."""
        kp = KeyPair.generate()
        signed = _make_writ().sign(kp.private_key)

        tampered = signed.model_copy(update={"working_directory": "/evil"})
        assert tampered.verify(kp.public_key) is False

    def test_wrong_public_key_fails(self):
        """Signature from one key doesn't verify with different key."""
        kp1 = KeyPair.generate()
        kp2 = KeyPair.generate()
        signed = _make_writ().sign(kp1.private_key)
        assert signed.verify(kp2.public_key) is False

    def test_modifying_votes_does_not_break_signature(self):
        """member_votes are advisory — changing them doesn't affect verification."""
        kp = KeyPair.generate()
        signed = _make_writ(member_votes=[]).sign(kp.private_key)

        with_votes = signed.model_copy(
            update={
                "member_votes": [
                    VoteSummary(member_id="m1", decision="approve"),
                ]
            }
        )
        assert with_votes.verify(kp.public_key) is True

    def test_packaged_file_is_signed(self):
        kp = KeyPair.generate()
        content = base64.b64encode(b"#!/bin/sh\necho signed\n").decode()
        packaged_file = PackagedFile(path="script.sh", content_base64=content, mode="0755")
        signed = _make_writ(packaged_files=[packaged_file]).sign(kp.private_key)

        tampered_file = PackagedFile(
            path="script.sh",
            content_base64=base64.b64encode(b"#!/bin/sh\necho tampered\n").decode(),
            mode="0755",
        )
        tampered = signed.model_copy(update={"packaged_files": [tampered_file]})

        assert signed.verify(kp.public_key) is True
        assert tampered.verify(kp.public_key) is False


class TestExpiration:
    def test_not_expired(self):
        writ = _make_writ(expires_at=time.time() + 300)
        assert writ.is_expired() is False

    def test_expired(self):
        writ = _make_writ(expires_at=time.time() - 1)
        assert writ.is_expired() is True

    def test_expired_with_explicit_now(self):
        writ = _make_writ(issued_at=1000.0, expires_at=2000.0)
        assert writ.is_expired(now=1500.0) is False
        assert writ.is_expired(now=2001.0) is True

    def test_exact_boundary(self):
        """Writ at exactly expires_at is not expired (> not >=)."""
        writ = _make_writ(expires_at=1000.0)
        assert writ.is_expired(now=1000.0) is False
        assert writ.is_expired(now=1000.001) is True


class TestVoteSummary:
    def test_basic(self):
        vs = VoteSummary(member_id="m1", decision="approve")
        assert vs.member_id == "m1"
        assert vs.risk_concerns == []

    def test_with_concerns(self):
        vs = VoteSummary(
            member_id="m2", decision="deny", risk_concerns=["too broad", "no rollback"]
        )
        assert len(vs.risk_concerns) == 2
