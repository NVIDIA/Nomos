# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Concurrency gate: many writers, one canonical chain, no forks, no gaps.

This is the proof required before council replicas can scale horizontally:
concurrent writers must not fork the chain or leave gaps in it.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from nomos.level_1.ledger import LedgerChain, LedgerEntryType
from tests.postgres_test_support import (
    ensure_migrated_and_clean,
    postgres_dsn_for_tests,
    postgres_unavailable_reason,
)

_PG_SKIP = postgres_unavailable_reason()
pytestmark = pytest.mark.skipif(_PG_SKIP is not None, reason=_PG_SKIP or "")

CHAIN_ID = "ledger-council-concurrency"
WRITERS = 8
APPENDS_PER_WRITER = 25


def _make_chain(pool) -> LedgerChain:
    from nomos.level_1.ledger.postgres_storage import PostgresLedgerStorage

    storage = PostgresLedgerStorage(pool, CHAIN_ID, council_id="council-concurrency")
    return LedgerChain(storage=storage, chain_id=CHAIN_ID)


def test_concurrent_writers_produce_one_canonical_chain():
    from psycopg_pool import ConnectionPool

    dsn = postgres_dsn_for_tests()
    ensure_migrated_and_clean(dsn)

    with ConnectionPool(dsn, min_size=2, max_size=WRITERS + 2, timeout=10, open=True) as pool:
        # every writer is its own LedgerChain instance, as separate council
        # replicas would be; they race initialize() too
        chains = [_make_chain(pool) for _ in range(WRITERS)]

        def run_writer(writer_index: int) -> int:
            chain = chains[writer_index]
            chain.initialize(creator_id=f"replica-{writer_index}")
            for n in range(APPENDS_PER_WRITER):
                chain.append(
                    LedgerEntryType.INTENT_SUBMITTED,
                    {"intent_id": f"w{writer_index}-{n}", "writer": writer_index},
                )
            return writer_index

        with ThreadPoolExecutor(max_workers=WRITERS) as executor:
            results = list(executor.map(run_writer, range(WRITERS)))
        assert sorted(results) == list(range(WRITERS))

        verifier_chain = _make_chain(pool)
        total = WRITERS * APPENDS_PER_WRITER  # + genesis
        # exact length: every append landed exactly once
        assert verifier_chain.length() == total + 1
        assert verifier_chain.get_head().sequence == total

        # contiguous sequences, intact hash links, valid content hashes
        entries = list(verifier_chain._storage.iterate())
        assert [e.sequence for e in entries] == list(range(total + 1))
        assert verifier_chain.validate() is True

        # chain head row matches the last entry
        assert verifier_chain.get_head().content_hash == entries[-1].content_hash

        # every writer's appends all landed
        for w in range(WRITERS):
            writer_entries = [e for e in entries if e.payload.get("writer") == w]
            assert len(writer_entries) == APPENDS_PER_WRITER
