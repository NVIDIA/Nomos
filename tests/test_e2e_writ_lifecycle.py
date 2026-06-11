# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
End-to-end tests for the Writ of Execution lifecycle.

Tests the full flow WITHOUT Redis or real LLM backends:
  1. Council key pair generation
  2. Writ minting and signing
  3. Serialization round-trip
  4. Executor verification and execution
  5. Tamper detection across the boundary
  6. Debate rounds (split vote → debate → re-vote → authorize → mint writ → execute)

This validates that the council and executor packages interoperate
correctly through the serialized writ blob.
"""

import base64
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from executors.docker.config import ExecutorConfig
from executors.docker.enforcer import WritEnforcer
from executors.docker.models import WritOfExecution as ExecutorWrit
from executors.docker.nonce_store import NonceStore
from nomos.auth.permissions import PermissionScope, categorize_intent
from nomos.auth.writ_factory import WritFactory
from nomos.config.settings import CouncilMemberConfig, CouncilSettings, OPAConfig
from nomos.crypto.key_manager import CouncilKeyManager
from nomos.level_0.crypto.signing import KeyPair
from nomos.level_0.types import Intent, PackagedFile
from nomos.level_0.types.writ import VoteSummary
from nomos.llm.base import LLMBackend, LLMResponse
from nomos.llm.council_agent import CouncilMemberAgent
from nomos.nat.functions import AuthorizeInput, CouncilService, ProposeIntentInput

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def council_keys(tmp_path):
    """Generate council key pair on disk."""
    mgr = CouncilKeyManager(str(tmp_path / "keys"))
    mgr.load_or_generate()
    return mgr


@pytest.fixture
def factory(council_keys):
    """WritFactory backed by real council keys, no Redis."""
    return WritFactory(
        council_key_pair=council_keys.key_pair,
        council_id="council-e2e-test",
        store=None,
    )


@pytest.fixture
def mock_nonce_store():
    store = MagicMock(spec=NonceStore)
    store.claim_nonce = AsyncMock(return_value=True)
    store.release_nonce = AsyncMock(return_value=True)
    store.report_outcome = AsyncMock()
    return store


@pytest.fixture
def enforcer(council_keys, mock_nonce_store):
    """WritEnforcer using the same council public key."""
    config = ExecutorConfig(
        allowed_working_directories=["/tmp", "/app/workspace"],
        max_command_timeout=30,
    )
    return WritEnforcer(
        public_key=council_keys.get_public_key_bytes(),
        nonce_store=mock_nonce_store,
        config=config,
    )


def _make_intent(command: str = "echo e2e-test") -> Intent:
    return Intent.create(
        agent_id="agent-e2e",
        action_type="system.info",
        description="End-to-end writ lifecycle test command",
        target_resource="/tmp",
        justification="E2E testing",
        command=command,
        parameters={"command": command},
    )


# ── E2E Tests ─────────────────────────────────────────────────────────


class TestFullWritLifecycle:
    """Complete lifecycle: mint → serialize → deserialize → verify → execute."""

    @pytest.mark.asyncio
    async def test_mint_serialize_verify_execute(self, factory, enforcer, council_keys):
        """The happy path: council mints, agent receives blob, executor runs."""
        intent = _make_intent("echo hello-from-writ")

        # Council mints and signs the writ
        writ = await factory.mint_writ(
            intent=intent,
            target_agent_id="agent-e2e",
            threshold_used="quorum",
            member_votes=[
                VoteSummary(member_id="member-1", decision="approve"),
                VoteSummary(member_id="member-2", decision="approve"),
            ],
            chairman_risk_level="low",
            working_directory="/tmp",
        )

        # Verify the writ is properly signed
        assert writ.signature != ""
        assert writ.verify(council_keys.get_public_key_bytes())

        # Serialize to blob (what the agent receives)
        blob = WritFactory.serialize_writ(writ)
        assert isinstance(blob, str)
        assert len(blob) > 0

        # Executor deserializes and verifies
        result = await enforcer.execute_writ(blob)

        assert result.success is True
        assert result.verification_stage == "passed"
        assert result.exit_code == 0
        assert "hello-from-writ" in result.stdout

    @pytest.mark.asyncio
    async def test_packaged_writ_mint_serialize_verify_execute(
        self,
        factory,
        council_keys,
        mock_nonce_store,
        tmp_path,
    ):
        """Packaged files are signed by council and verified by executor."""
        script = b"#!/bin/sh\necho packaged-e2e\n"
        packaged_file = PackagedFile(
            path="scripts/e2e-packaged.sh",
            content_base64=base64.b64encode(script).decode(),
            mode="0700",
            description="E2E packaged script",
        )
        intent = _make_intent("/bin/sh scripts/e2e-packaged.sh")
        intent.packaged_files.append(packaged_file)

        writ = await factory.mint_writ(
            intent=intent,
            target_agent_id="agent-e2e",
            threshold_used="quorum",
            member_votes=[VoteSummary(member_id="member-1", decision="approve")],
            chairman_risk_level="low",
            working_directory=str(tmp_path),
        )
        blob = WritFactory.serialize_writ(writ)
        executor_writ = ExecutorWrit.model_validate_json(base64.b64decode(blob))

        assert writ.canonical_bytes() == executor_writ.canonical_bytes()
        assert executor_writ.verify(council_keys.get_public_key_bytes()) is True

        enforcer = WritEnforcer(
            public_key=council_keys.get_public_key_bytes(),
            nonce_store=mock_nonce_store,
            config=ExecutorConfig(
                allowed_working_directories=[str(tmp_path)],
                max_command_timeout=30,
            ),
        )
        result = await enforcer.execute_writ(blob)

        assert result.success is True
        assert result.verification_stage == "passed"
        assert "packaged-e2e" in result.stdout
        assert (tmp_path / "scripts" / "e2e-packaged.sh").read_bytes() == script

    @pytest.mark.asyncio
    async def test_roundtrip_preserves_all_fields(self, factory, council_keys):
        """Serialize → deserialize preserves all writ fields."""
        intent = _make_intent("ls /tmp")
        writ = await factory.mint_writ(
            intent=intent,
            target_agent_id="agent-e2e",
            threshold_used="unanimous",
            member_votes=[VoteSummary(member_id="m1", decision="approve")],
            chairman_risk_level="high",
            working_directory="/tmp",
        )

        blob = WritFactory.serialize_writ(writ)
        restored = WritFactory.deserialize_writ(blob)

        assert restored.writ_id == writ.writ_id
        assert restored.command == writ.command
        assert restored.nonce == writ.nonce
        assert restored.signature == writ.signature
        assert restored.target_agent_id == writ.target_agent_id
        assert restored.threshold_used == "unanimous"
        assert restored.chairman_risk_level == "high"
        # member_votes are intentionally stripped from serialized blobs
        # (not covered by signature — see security analysis)
        assert restored.member_votes == []

    @pytest.mark.asyncio
    async def test_key_persistence_across_sessions(self, tmp_path):
        """Keys persist across CouncilKeyManager sessions."""
        key_dir = str(tmp_path / "keys")

        # Session 1: generate
        mgr1 = CouncilKeyManager(key_dir)
        kp1 = mgr1.load_or_generate()

        # Mint a writ
        factory = WritFactory(council_key_pair=kp1, council_id="c1")
        intent = _make_intent("echo persist-test")
        writ = await factory.mint_writ(
            intent=intent,
            target_agent_id="a",
            threshold_used="quorum",
            member_votes=[],
            chairman_risk_level="low",
            working_directory="/tmp",
        )
        blob = WritFactory.serialize_writ(writ)

        # Session 2: load existing keys
        mgr2 = CouncilKeyManager(key_dir)
        kp2 = mgr2.load_or_generate()
        assert kp2.public_key == kp1.public_key

        # Verify with reloaded public key
        restored = WritFactory.deserialize_writ(blob)
        assert restored.verify(kp2.public_key)


class TestCrossBoundaryTamperDetection:
    """Verify that tampering between council and executor is detected."""

    @pytest.mark.asyncio
    async def test_tampered_command_caught_by_executor(self, factory, enforcer):
        """Agent modifies command in the blob → executor rejects."""
        intent = _make_intent("echo safe")
        writ = await factory.mint_writ(
            intent=intent,
            target_agent_id="a",
            threshold_used="quorum",
            member_votes=[],
            chairman_risk_level="low",
            working_directory="/tmp",
        )

        # Tamper: decode blob, modify command, re-encode
        blob = WritFactory.serialize_writ(writ)
        json_bytes = base64.b64decode(blob)
        import json

        data = json.loads(json_bytes)
        data["command"] = "echo HACKED"
        tampered_blob = base64.b64encode(json.dumps(data).encode()).decode()

        result = await enforcer.execute_writ(tampered_blob)
        assert result.success is False
        assert result.verification_stage == "signature"

    @pytest.mark.asyncio
    async def test_tampered_expires_at_caught(self, factory, enforcer):
        """Agent extends expiration → executor rejects."""
        intent = _make_intent("echo test")
        writ = await factory.mint_writ(
            intent=intent,
            target_agent_id="a",
            threshold_used="quorum",
            member_votes=[],
            chairman_risk_level="low",
            working_directory="/tmp",
        )

        blob = WritFactory.serialize_writ(writ)
        import json

        data = json.loads(base64.b64decode(blob))
        data["expires_at"] = time.time() + 999999
        tampered = base64.b64encode(json.dumps(data).encode()).decode()

        result = await enforcer.execute_writ(tampered)
        assert result.verification_stage == "signature"

    @pytest.mark.asyncio
    async def test_tampered_nonce_caught(self, factory, enforcer):
        """Agent replaces nonce → executor rejects."""
        intent = _make_intent("echo test")
        writ = await factory.mint_writ(
            intent=intent,
            target_agent_id="a",
            threshold_used="quorum",
            member_votes=[],
            chairman_risk_level="low",
            working_directory="/tmp",
        )

        blob = WritFactory.serialize_writ(writ)
        import json

        data = json.loads(base64.b64decode(blob))
        data["nonce"] = "00" * 32
        tampered = base64.b64encode(json.dumps(data).encode()).decode()

        result = await enforcer.execute_writ(tampered)
        assert result.verification_stage == "signature"

    @pytest.mark.asyncio
    async def test_wrong_council_key_rejected(self, factory, mock_nonce_store):
        """Writ signed by one council, verified by executor with different key."""
        other_kp = KeyPair.generate(key_id="rogue-council")
        config = ExecutorConfig(allowed_working_directories=["/tmp"])
        enforcer = WritEnforcer(
            public_key=other_kp.public_key,
            nonce_store=mock_nonce_store,
            config=config,
        )

        intent = _make_intent("echo test")
        writ = await factory.mint_writ(
            intent=intent,
            target_agent_id="a",
            threshold_used="quorum",
            member_votes=[],
            chairman_risk_level="low",
            working_directory="/tmp",
        )
        blob = WritFactory.serialize_writ(writ)

        result = await enforcer.execute_writ(blob)
        assert result.verification_stage == "signature"


class TestReplayPrevention:
    @pytest.mark.asyncio
    async def test_same_blob_second_time_rejected(self, factory, council_keys):
        """Replaying the same writ blob is rejected by nonce store."""
        nonce_store = MagicMock(spec=NonceStore)
        nonce_store.report_outcome = AsyncMock()
        # First claim succeeds, second fails
        nonce_store.claim_nonce = AsyncMock(side_effect=[True, False])
        nonce_store.release_nonce = AsyncMock()

        config = ExecutorConfig(allowed_working_directories=["/tmp"])
        enforcer = WritEnforcer(
            public_key=council_keys.get_public_key_bytes(),
            nonce_store=nonce_store,
            config=config,
        )

        intent = _make_intent("echo replay-test")
        writ = await factory.mint_writ(
            intent=intent,
            target_agent_id="a",
            threshold_used="quorum",
            member_votes=[],
            chairman_risk_level="low",
            working_directory="/tmp",
        )
        blob = WritFactory.serialize_writ(writ)

        # First execution succeeds
        r1 = await enforcer.execute_writ(blob)
        assert r1.success is True

        # Second execution (replay) fails
        r2 = await enforcer.execute_writ(blob)
        assert r2.success is False
        assert r2.verification_stage == "nonce"


class TestPermissionCategorization:
    """Verify categorize_intent works correctly in the authorization flow."""

    def test_read_command_needs_shell_read(self):
        intent = _make_intent("ls /tmp")
        perms = categorize_intent(intent)
        assert PermissionScope.SHELL_READ in perms
        assert PermissionScope.SHELL_WRITE not in perms

    def test_delete_command_needs_write_and_delete(self):
        intent = _make_intent("rm -rf /var/data")
        perms = categorize_intent(intent)
        assert PermissionScope.SHELL_WRITE in perms
        assert PermissionScope.FILE_DELETE in perms

    def test_kubectl_apply_needs_infra_deploy(self):
        intent = _make_intent("kubectl apply -f deploy.yaml")
        perms = categorize_intent(intent)
        assert PermissionScope.INFRA_DEPLOY in perms


class TestExpirationEnforcement:
    @pytest.mark.asyncio
    async def test_expired_writ_rejected_by_executor(self, factory, enforcer):
        """Writ that expires before reaching executor is rejected."""
        _make_intent("echo expired")

        # Mint with critical risk (30s TTL) but fake issued_at in the past
        from nomos.level_0.types.writ import WritOfExecution as NomosWrit

        now = time.time()
        kp = factory._signer.key_pair
        nomos_writ = NomosWrit(
            writ_id="writ-expired",
            intent_hash="ab" * 32,
            command="echo expired",
            working_directory="/tmp",
            target_agent_id="a",
            issued_at=now - 600,
            expires_at=now - 1,  # Already expired
            max_executions=1,
            nonce="ee" * 32,
            council_id="c",
            threshold_used="quorum",
            chairman_risk_level="critical",
        )
        signed = nomos_writ.sign(kp.private_key)
        blob = base64.b64encode(signed.model_dump_json().encode()).decode()

        result = await enforcer.execute_writ(blob)
        assert result.success is False
        assert result.verification_stage == "temporal"


# ── E2E Debate Round Helpers ─────────────────────────────────────────


class _MockBackend(LLMBackend):
    """Mock LLM backend for initial council member evaluation."""

    def __init__(self, decision: str):
        self._response = json.dumps(
            {
                "decision": decision,
                "reasoning": f"Mock agent says {decision}",
                "risk_concerns": [],
                "conditions": [],
            }
        )

    async def complete(self, messages, temperature=0.0, max_tokens=1024, response_format=None):
        return LLMResponse(content=self._response, model="mock")

    def provider_name(self):
        return "mock"


class _FlipBackend(_MockBackend):
    """Backend that returns deny on the first call, approve on all subsequent calls.

    Used to simulate a member that changes its vote during debate.
    """

    def __init__(self):
        self._calls = 0

    async def complete(self, messages, temperature=0.0, max_tokens=1024, response_format=None):
        self._calls += 1
        decision = "deny" if self._calls == 1 else "approve"
        return LLMResponse(
            content=json.dumps(
                {
                    "decision": decision,
                    "reasoning": f"call {self._calls}: {decision}",
                    "risk_concerns": [],
                    "conditions": [],
                }
            ),
            model="mock",
        )

    def provider_name(self):
        return "mock"


def _make_debate_service(decisions: list[str]) -> CouncilService:
    """Create a CouncilService with mock agents returning the given decisions."""
    settings = CouncilSettings(
        council_id="e2e-debate-council",
        require_auth=False,
        require_justification=True,
        llm_backends={},
        members=[],
        opa=OPAConfig(enabled=False),
    )
    service = CouncilService(settings)

    from nomos.level_0.types import CouncilMember

    for i, decision in enumerate(decisions):
        backend = _MockBackend(decision=decision)
        config = CouncilMemberConfig(
            id=f"e2e-member-{i}",
            backend="mock",
            weight=1,
            display_name=f"E2E Member {i}",
            priorities=[f"Always {decision}"],
        )
        agent = CouncilMemberAgent(member_config=config, backend=backend)
        service._agents.append(agent)

        member = CouncilMember(
            member_id=config.id,
            public_key_hex="",
            display_name=config.display_name,
            weight=config.weight,
        )
        service.council.register_council_member(member)

    return service


# ── E2E Debate Round Tests ───────────────────────────────────────────


def _make_flip_debate_service(approve_ids: list[int], flip_ids: list[int]) -> CouncilService:
    """Create a CouncilService where some members always approve/deny and
    some members use _FlipBackend (deny on first call, approve thereafter)."""
    settings = CouncilSettings(
        council_id="e2e-debate-council",
        require_auth=False,
        require_justification=True,
        llm_backends={},
        members=[],
        opa=OPAConfig(enabled=False),
    )
    service = CouncilService(settings)

    from nomos.level_0.types import CouncilMember

    for i in approve_ids:
        backend = _MockBackend(decision="approve")
        config = CouncilMemberConfig(
            id=f"e2e-member-{i}",
            backend="mock",
            weight=1,
            display_name=f"E2E Member {i}",
            priorities=["approve"],
        )
        service._agents.append(CouncilMemberAgent(member_config=config, backend=backend))
        service.council.register_council_member(
            CouncilMember(
                member_id=f"e2e-member-{i}",
                public_key_hex="",
                display_name=f"E2E Member {i}",
                weight=1,
            )
        )
    for i in flip_ids:
        backend = _FlipBackend()
        config = CouncilMemberConfig(
            id=f"e2e-member-{i}",
            backend="mock",
            weight=1,
            display_name=f"E2E Member {i}",
            priorities=["flip"],
        )
        service._agents.append(CouncilMemberAgent(member_config=config, backend=backend))
        service.council.register_council_member(
            CouncilMember(
                member_id=f"e2e-member-{i}",
                public_key_hex="",
                display_name=f"E2E Member {i}",
                weight=1,
            )
        )
    return service


class TestDebateE2E:
    """End-to-end: split vote → debate → re-vote through propose_intent.

    Each council member uses its own LLM backend for debate rounds.
    _MockBackend always returns the same decision; _FlipBackend returns
    deny on the first call (initial eval) and approve on subsequent calls
    (debate rounds), simulating a member that is persuaded.
    """

    @pytest.mark.asyncio
    async def test_split_vote_debate_reaches_consensus(self):
        """Split vote (2 approve, 1 deny/flip) → debate → all approve → approved."""
        # Members 0 and 1 always approve; member 2 denies then flips to approve
        service = _make_flip_debate_service(approve_ids=[0, 1], flip_ids=[2])

        result = await service.propose_intent(
            ProposeIntentInput(
                agent_id="agent-e2e",
                action_type="file.read",
                description="Read config file after debate",
                target_resource="/tmp/config.yaml",
                justification="E2E debate consensus test",
            ),
            enable_debate=True,
            max_debate_rounds=1,
        )

        assert result.success is True
        assert result.debate_rounds == 1
        assert result.state == "approved"
        votes_by_id = {v.member_id: v for v in result.council_members}
        assert votes_by_id["e2e-member-2"].changed_vote is True
        assert votes_by_id["e2e-member-0"].changed_vote is False

    @pytest.mark.asyncio
    async def test_debate_stays_split_denied(self):
        """Split vote → debate → still split → denied."""
        # Both members keep their decisions through debate
        service = _make_debate_service(["approve", "deny"])

        result = await service.propose_intent(
            ProposeIntentInput(
                agent_id="agent-e2e",
                action_type="file.delete",
                description="Delete temp files from workspace",
                target_resource="/tmp/old-data",
                justification="E2E debate split test",
            ),
            enable_debate=True,
            max_debate_rounds=2,
        )

        assert result.success is True
        assert result.debate_rounds == 2
        assert result.state == "denied"

    @pytest.mark.asyncio
    async def test_unanimous_skips_debate(self):
        """All agree in Round 1 → no debate triggered."""
        service = _make_debate_service(["approve", "approve"])

        result = await service.propose_intent(
            ProposeIntentInput(
                agent_id="agent-e2e",
                action_type="system.info",
                description="Get system information for monitoring",
                target_resource="/proc/loadavg",
                justification="E2E unanimity test",
            ),
            enable_debate=True,
            max_debate_rounds=3,
        )

        assert result.success is True
        assert result.debate_rounds == 0
        assert result.state == "approved"

    @pytest.mark.asyncio
    async def test_debate_flips_denial_to_approval(self):
        """Majority deny → debate flips deniers to approve."""
        # Members 1 and 2 deny then flip; member 0 always approves
        service = _make_flip_debate_service(approve_ids=[0], flip_ids=[1, 2])

        result = await service.propose_intent(
            ProposeIntentInput(
                agent_id="agent-e2e",
                action_type="file.write",
                description="Write deployment config to workspace",
                target_resource="/tmp/deploy.yaml",
                justification="E2E debate flip test",
            ),
            enable_debate=True,
            max_debate_rounds=1,
        )

        assert result.success is True
        assert result.debate_rounds == 1
        assert result.state == "approved"
        changed = [v for v in result.council_members if v.changed_vote]
        assert len(changed) == 2


class TestDebateAuthorizeE2E:
    """E2E: debate through authorize flow → mint writ → verify on executor."""

    @pytest.mark.asyncio
    async def test_authorize_debate_mints_valid_writ(self, council_keys):
        """Split vote → debate → consensus → writ minted and verifiable."""
        # Member 0 always approves; member 1 denies then flips to approve
        service = _make_flip_debate_service(approve_ids=[0], flip_ids=[1])
        service._key_manager = council_keys
        service._settings.council_id = "e2e-debate-council"

        result = await service.authorize(
            AuthorizeInput(
                agent_id="agent-e2e",
                command="/bin/echo debate-authorized",
                working_directory="/tmp",
                action_type="system.exec",
                description="Execute command after debate authorization",
                target_resource="/tmp",
                justification="E2E debate authorize test",
            ),
            enable_debate=True,
            max_debate_rounds=1,
        )

        assert result.status == "approved"
        assert result.debate_rounds == 1
        assert result.writ is not None
        assert result.writ_id != ""

        restored = WritFactory.deserialize_writ(result.writ)
        assert restored.command == "/bin/echo debate-authorized"
        assert restored.verify(council_keys.get_public_key_bytes())

    @pytest.mark.asyncio
    async def test_authorize_debate_denied_no_writ(self, council_keys):
        """Split vote → debate → still denied → no writ minted."""
        # Both members keep their decisions → stays split → denied
        service = _make_debate_service(["approve", "deny"])
        service._key_manager = council_keys

        result = await service.authorize(
            AuthorizeInput(
                agent_id="agent-e2e",
                command="/bin/rm -rf /important",
                working_directory="/tmp",
                action_type="file.delete",
                description="Delete important files from production",
                target_resource="/important",
                justification="E2E debate deny authorize test",
            ),
            enable_debate=True,
            max_debate_rounds=1,
        )

        assert result.status == "denied"
        assert result.debate_rounds == 1
        assert result.writ is None

    @pytest.mark.asyncio
    async def test_debate_writ_executes_on_enforcer(self, council_keys, mock_nonce_store):
        """Full E2E: split → debate → approve → mint writ → executor runs it."""
        # Member 0 always approves; member 1 denies then flips to approve
        service = _make_flip_debate_service(approve_ids=[0], flip_ids=[1])
        service._key_manager = council_keys
        service._settings.council_id = "e2e-debate-council"

        result = await service.authorize(
            AuthorizeInput(
                agent_id="agent-e2e",
                command="/bin/echo debate-e2e-full",
                working_directory="/tmp",
                action_type="system.exec",
                description="Full E2E debate to executor pipeline test",
                target_resource="/tmp",
                justification="E2E full pipeline test",
            ),
            enable_debate=True,
            max_debate_rounds=1,
        )

        assert result.status == "approved"
        assert result.debate_rounds == 1

        # Feed the writ blob to the executor
        config = ExecutorConfig(
            allowed_working_directories=["/tmp", "/app/workspace"],
            max_command_timeout=30,
        )
        enforcer = WritEnforcer(
            public_key=council_keys.get_public_key_bytes(),
            nonce_store=mock_nonce_store,
            config=config,
        )

        exec_result = await enforcer.execute_writ(result.writ)
        assert exec_result.success is True
        assert exec_result.verification_stage == "passed"
        assert exec_result.exit_code == 0
        assert "debate-e2e-full" in exec_result.stdout
