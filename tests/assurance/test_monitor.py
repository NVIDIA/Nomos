# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Security and restart contracts for the asynchronous ledger observer."""

import copy

import httpx
import pytest

from assurance.monitor import IntegrityError, LedgerSource, Monitor, Store, verify_batch
from nomos.level_0.crypto import KeyPair, Signer
from nomos.level_1.ledger.chain import LedgerChain
from nomos.level_1.ledger.entry import LedgerEntryType


@pytest.fixture
def ledger():
    key = KeyPair.generate("test-signer")
    chain = LedgerChain(signer=Signer(key), chain_id="test-chain")
    chain.initialize("tests")
    for i in range(6):
        chain.append(LedgerEntryType.POLICY_UPDATED, {"test": i})
    return chain, {key.key_id: key.public_key_hex()}


def rows(chain, start=0, stop=7):
    return [copy.deepcopy(chain.get(i).to_dict()) for i in range(start, stop)]


def test_verify_signature_hash_and_continuity(ledger):
    chain, keys = ledger
    verified = verify_batch(rows(chain), -1, None, "test-chain", keys)
    assert len(verified) == 7
    assert verified[-1]["content_hash"] == str(chain.get_head_hash())


@pytest.mark.parametrize(
    "corruption",
    ["payload", "signature", "unsigned", "key", "algorithm", "gap", "chain", "timestamp"],
)
def test_untrusted_entries_fail_closed(ledger, corruption):
    chain, keys = ledger
    batch = rows(chain)
    if corruption == "payload":
        batch[2]["payload"]["test"] = "tampered"
    elif corruption == "signature":
        batch[2]["signatures"][0]["signature_hex"] = "00" * 64
    elif corruption == "unsigned":
        batch[2]["signatures"] = []
    elif corruption == "key":
        keys = {}
    elif corruption == "algorithm":
        batch[2]["signatures"][0]["algorithm"] = "none"
    elif corruption == "gap":
        batch.pop(3)
    elif corruption == "chain":
        batch[0]["payload"]["chain_id"] = "another-chain"
    else:
        batch[2]["timestamp"] = "2026-01-01T00:00:00"
    with pytest.raises(IntegrityError):
        verify_batch(batch, -1, None, "test-chain", keys)


def test_unsigned_metadata_removed(ledger):
    chain, keys = ledger
    batch = rows(chain)
    batch[2]["metadata"] = {"agent_id": "framed-agent"}
    verified = verify_batch(batch, -1, None, "test-chain", keys)
    assert "metadata" not in verified[2]


class FakeSource:
    def __init__(self, chain):
        self.chain = chain
        self.tamper = False
        self.omit = False
        self.short = False

    async def page(self, cursor=None, limit=100):
        head = self.chain.get_head().sequence - (3 if self.short else 0)
        end = head if cursor is None else min(cursor, head)
        entries = rows(self.chain, max(0, end - limit + 1), end + 1)[::-1]
        if self.omit:
            entries = entries[1:]
        if self.tamper and entries:
            entries[0]["payload"]["tampered"] = True
        return {"chain_id": "test-chain", "head_sequence": head, "entries": entries}

    async def get(self, sequence):
        entry = copy.deepcopy(self.chain.get(sequence).to_dict())
        if self.tamper:
            entry["payload"]["tampered"] = True
        return entry


@pytest.mark.asyncio
async def test_batched_ingestion_restart_and_replay(ledger, tmp_path):
    chain, keys = ledger
    db = tmp_path / "monitor.db"
    source = FakeSource(chain)
    store = Store(db, "test-chain")
    monitor = Monitor(source, store, keys, batch_size=2)
    for _ in range(4):
        await monitor.ingest()
    assert store.checkpoint() == (6, str(chain.get_head_hash()))
    await monitor.ingest()
    assert len(store.events()) == 7
    recovered = Store(db, "test-chain")
    assert recovered.checkpoint() == store.checkpoint()
    assert recovered.analysis_sequence() == -1  # durable ingest does not claim analysis
    with pytest.raises(ValueError, match="chain"):
        Store(db, "different-chain")


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["tamper", "omit", "short"])
async def test_bad_source_does_not_advance_checkpoint(ledger, tmp_path, fault):
    chain, keys = ledger
    source = FakeSource(chain)
    store = Store(tmp_path / "state.db", "test-chain")
    monitor = Monitor(source, store, keys, batch_size=3)
    await monitor.ingest()
    await monitor.ingest()
    before = store.checkpoint()
    setattr(source, fault, True)
    with pytest.raises(IntegrityError):
        await monitor.ingest()
    assert store.checkpoint() == before


@pytest.mark.asyncio
async def test_api_wrapper_and_authentication_header():
    async def respond(request):
        assert request.headers["Authorization"] == "Bearer fixture-token"
        return httpx.Response(
            200, json={"result": '{"chain_id":"c","head_sequence":0,"entries":[]}'}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        source = LedgerSource("http://council", "c", "fixture-token", client=client)
        assert (await source.page())["chain_id"] == "c"


@pytest.mark.asyncio
async def test_cycle_only_ingests_and_retains_integrity_incident(ledger, tmp_path):
    chain, keys = ledger
    source = FakeSource(chain)
    store = Store(tmp_path / "state.db", "test-chain")
    monitor = Monitor(source, store, keys)
    await monitor.ingest()  # simulated crash before analysis
    resumed = Monitor(source, Store(store.path, "test-chain"), keys)
    await resumed.cycle()
    assert store.analysis_sequence() == -1
    source.tamper = True
    for _ in range(2):
        with pytest.raises(IntegrityError):
            await resumed.cycle()
    assert store.setting("status")["state"] == "integrity_error"
    incidents = [f for f in store.findings() if f["kind"] == "ledger_integrity_failure"]
    assert len(incidents) == 1
    assert incidents[0]["agent_id"] == "unattributed"
    assert incidents[0]["evidence_role"] == "last trusted checkpoint; rejected data is not evidence"
    source.tamper = False
    await resumed.cycle()
    assert store.setting("status")["state"] == "ok"
    assert len(store.findings()) == 1


@pytest.mark.asyncio
async def test_backlog_limit_holds_ingestion_until_streams_catch_up(ledger, tmp_path):
    chain, keys = ledger
    store = Store(tmp_path / "state.db", "test-chain")
    monitor = Monitor(FakeSource(chain), store, keys, batch_size=2)
    await monitor.cycle()
    checkpoint = store.checkpoint()
    await monitor.cycle(backlog=2, max_backlog=2)
    assert store.checkpoint() == checkpoint
    assert store.setting("status")["error_type"] == "BacklogLimit"
    assert store.setting("status")["state"] == "worker_error"
    assert store.analysis_sequence() == -1
    await monitor.cycle(backlog=1, max_backlog=2)
    assert store.checkpoint()[0] > checkpoint[0]
    assert store.setting("status")["state"] == "ok"
