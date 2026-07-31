# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for the PostgreSQL ledger backend."""

from __future__ import annotations

import pytest

from nomos.crypto.signer_provider import InMemorySigner
from nomos.level_0.crypto.signing import KeyPair
from nomos.level_1.ledger import (
    LedgerAppendError,
    LedgerChain,
    LedgerEntryType,
)
from tests.postgres_test_support import (
    ensure_migrated_and_clean,
    postgres_dsn_for_tests,
    postgres_unavailable_reason,
)

_PG_SKIP = postgres_unavailable_reason()
pytestmark = pytest.mark.skipif(_PG_SKIP is not None, reason=_PG_SKIP or "")

CHAIN_ID = "ledger-council-pg-test"


@pytest.fixture
def pool():
    from psycopg_pool import ConnectionPool

    dsn = postgres_dsn_for_tests()
    ensure_migrated_and_clean(dsn)
    with ConnectionPool(dsn, min_size=1, max_size=4, timeout=5, open=True) as pool:
        yield pool


def _chain(pool, signer=None) -> LedgerChain:
    from nomos.level_1.ledger.postgres_storage import PostgresLedgerStorage

    storage = PostgresLedgerStorage(pool, CHAIN_ID, council_id="council-pg-test")
    return LedgerChain(storage=storage, signer=signer, chain_id=CHAIN_ID)


class TestPostgresLedgerRoundTrip:
    def test_initialize_append_get_head_iterate_validate(self, pool):
        signer = InMemorySigner(KeyPair.generate(key_id="council-pg-test"))
        chain = _chain(pool, signer)

        genesis = chain.initialize(creator_id="council-pg-test")
        assert genesis.sequence == 0
        assert genesis.previous_hash is None
        assert genesis.signatures

        first = chain.append(LedgerEntryType.INTENT_SUBMITTED, {"intent_id": "i-1"})
        second = chain.append(
            LedgerEntryType.INTENT_EVALUATED,
            {"intent_id": "i-1", "decision": "allow"},
            metadata={"writer": "test"},
        )

        assert first.sequence == 1
        assert second.sequence == 2
        assert second.previous_hash == first.content_hash
        assert chain.length() == 3
        assert chain.get_head().sequence == 2
        assert chain.get(1).payload == {"intent_id": "i-1"}
        assert [e.sequence for e in chain._storage.iterate()] == [0, 1, 2]
        assert chain.validate() is True
        # signatures round-trip and verify
        chain.register_verifier_key(signer.key_id, signer.public_key_bytes())
        assert chain.verify_signatures(second)

    def test_append_before_initialize_fails_closed(self, pool):
        chain = _chain(pool)
        with pytest.raises(LedgerAppendError, match="not initialized"):
            chain.append(LedgerEntryType.INTENT_SUBMITTED, {"intent_id": "i-x"})

    def test_query_helpers(self, pool):
        chain = _chain(pool)
        chain.initialize(creator_id="c")
        chain.append(LedgerEntryType.INTENT_SUBMITTED, {"intent_id": "i-1"})
        chain.append(LedgerEntryType.INTENT_SUBMITTED, {"intent_id": "i-2"})
        assert len(chain.query_by_type(LedgerEntryType.INTENT_SUBMITTED)) == 2
        assert len(chain.query_by_intent("i-2")) == 1


class TestIdempotentGenesis:
    def test_two_replicas_share_one_genesis(self, pool):
        replica_a = _chain(pool)
        replica_b = _chain(pool)

        genesis_a = replica_a.initialize(creator_id="replica-a")
        genesis_b = replica_b.initialize(creator_id="replica-b")

        assert genesis_a.content_hash == genesis_b.content_hash
        assert genesis_b.payload["creator_id"] == "replica-a"  # loser got winner's genesis
        assert replica_a.length() == 1
        # both replicas can append against the shared chain
        replica_a.append(LedgerEntryType.INTENT_SUBMITTED, {"intent_id": "a"})
        replica_b.append(LedgerEntryType.INTENT_SUBMITTED, {"intent_id": "b"})
        assert replica_b.validate() is True
        assert replica_a.length() == 3


class TestFailClosed:
    def test_lock_holdout_exhausts_retries_with_no_partial_rows(self, pool):
        import psycopg

        from nomos.level_1.ledger.postgres_storage import (
            LedgerRetryPolicy,
            PostgresLedgerStorage,
        )

        chain = _chain(pool)
        chain.initialize(creator_id="c")

        fast_retry = LedgerRetryPolicy(
            max_attempts=2, base_delay_s=0.01, max_delay_s=0.02, total_deadline_s=0.5
        )
        contender = LedgerChain(
            storage=PostgresLedgerStorage(
                pool,
                CHAIN_ID,
                council_id="council-pg-test",
                lock_timeout_ms=50,
                retry=fast_retry,
            ),
            chain_id=CHAIN_ID,
        )

        dsn = postgres_dsn_for_tests()
        with psycopg.connect(dsn) as blocker:
            blocker.execute(
                "SELECT 1 FROM ledger_chains WHERE chain_id = %s FOR UPDATE", (CHAIN_ID,)
            )
            # lock held in the open transaction; contender must fail closed
            with pytest.raises(LedgerAppendError):
                contender.append(LedgerEntryType.INTENT_SUBMITTED, {"intent_id": "blocked"})
            blocker.rollback()

        # no partial entry escaped a failed transaction
        assert chain.length() == 1
        assert chain.validate() is True
