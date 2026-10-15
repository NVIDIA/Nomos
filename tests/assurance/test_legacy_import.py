# SPDX-License-Identifier: Apache-2.0
"""The legacy importer preserves identities and fails before accepting corrupt state."""

import copy
import fnmatch
import json
import os

import pytest

from nomos.auth.account_integrity import sign_account_record, verify_account_record
from nomos.level_0.crypto import KeyPair, Signer
from nomos.level_1.ledger import LedgerChain, LedgerEntryType
from scripts.assurance_import_legacy import (
    ImportValidationError,
    import_snapshot,
    read_snapshot,
    validate_snapshot,
)

INTEGRITY_KEY = "test-only-account-integrity-key"


@pytest.fixture
def snapshot():
    key = KeyPair.generate("legacy-council")
    chain = LedgerChain(signer=Signer(key), chain_id="ledger-legacy")
    chain.initialize("legacy")
    chain.append(
        LedgerEntryType.INTENT_REVIEW_RECORDED,
        {"intent_id": "intent-1", "agent_id": "agent-1"},
        metadata={"unsigned_diagnostic": "preserve without trusting"},
    )
    record = {
        "token_hash": "test-token-hash",
        "display_name": "Test agent",
        "enabled": "1",
        "created_at": "1760000000.123",
        "updated_at": "1760000001.456",
        "role": "readonly",
        "custom": "keep this field",
    }
    record["_integrity_hmac"] = sign_account_record(
        key=INTEGRITY_KEY,
        agent_id="agent-1",
        account=record,
        permissions={"read"},
        councils={"legacy"},
    )
    executor = KeyPair.generate("executor-1")
    data = {
        "schema_version": 1,
        "ledger": {
            "chain_id": "ledger-legacy",
            "council_id": "legacy",
            "entries": [chain.get(i).to_dict() for i in range(chain.length())],
        },
        "redis": {
            "accounts": [
                {
                    "agent_id": "agent-1",
                    "record": record,
                    "permissions": ["read"],
                    "councils": ["legacy"],
                }
            ],
            "token_index": {"test-token-hash": "agent-1"},
            "writs": {"writ-1": {"writ_id": "writ-1", "nested": {"a": [1, 2]}}},
            "writ_outcomes": {
                "writ-1": {
                    "exit_code": "1",
                    "stdout_hash": "stdout-hash",
                    "stderr_hash": "stderr-hash",
                    "executor_id": "executor-1",
                    "executed_at": "1760000020.75",
                }
            },
            "intents": {"intent-1": {"intent_id": "intent-1", "agent_id": "agent-1"}},
            "agent_intents": {"agent-1": ["intent-1"]},
            "trusted_executors": [
                {
                    "executor_id": "executor-1",
                    "public_key_hex": executor.public_key_hex(),
                    "key_fingerprint": executor.fingerprint(),
                    "enabled": "1",
                    "registered_at": "1760000000.1",
                    "updated_at": "1760000001.2",
                    "metadata_json": '{"purpose": "test"}',
                }
            ],
        },
    }
    return data, {key.key_id: key.public_key_hex()}


def _validate(data, keys):
    return validate_snapshot(data, trusted_keys=keys, account_integrity_key=INTEGRITY_KEY)


def test_valid_snapshot_is_unchanged(snapshot):
    data, keys = snapshot
    before = copy.deepcopy(data)
    validated = _validate(data, keys)
    assert validated.ledger.entries == before["ledger"]["entries"]
    assert data == before


@pytest.mark.parametrize(
    "corruption",
    [
        "payload",
        "signature",
        "unsigned",
        "gap",
        "chain",
        "account",
        "permissions",
        "token_index",
        "duplicate_account",
        "duplicate_permission",
        "duplicate_executor",
        "executor_key",
        "queue_missing",
        "queue_duplicate",
        "queue_wrong_agent",
        "queue_incomplete",
    ],
)
def test_corrupt_or_inconsistent_snapshot_rejected(snapshot, corruption):
    data, keys = snapshot
    entries = data["ledger"]["entries"]
    state = data["redis"]
    if corruption == "payload":
        entries[1]["payload"]["agent_id"] = "framed-agent"
    elif corruption == "signature":
        entries[1]["signatures"][0]["signature_hex"] = "00" * 64
    elif corruption == "unsigned":
        entries[1]["signatures"] = []
    elif corruption == "gap":
        entries.append(copy.deepcopy(entries[1]))
    elif corruption == "chain":
        data["ledger"]["chain_id"] = "ledger-other"
    elif corruption == "account":
        state["accounts"][0]["record"]["role"] = "operator"
    elif corruption == "permissions":
        state["accounts"][0]["permissions"].append("admin")
    elif corruption == "token_index":
        state["token_index"]["stale-token"] = "agent-1"
    elif corruption == "duplicate_account":
        state["accounts"].append(copy.deepcopy(state["accounts"][0]))
    elif corruption == "duplicate_permission":
        state["accounts"][0]["permissions"].append("read")
    elif corruption == "duplicate_executor":
        state["trusted_executors"].append(copy.deepcopy(state["trusted_executors"][0]))
    elif corruption == "executor_key":
        state["trusted_executors"][0]["key_fingerprint"] = "00" * 32
    elif corruption == "queue_missing":
        state["agent_intents"]["agent-1"] = ["missing"]
    elif corruption == "queue_duplicate":
        state["agent_intents"]["agent-1"].append("intent-1")
    elif corruption == "queue_wrong_agent":
        state["agent_intents"] = {"agent-2": ["intent-1"]}
    else:
        state["agent_intents"] = {}
    with pytest.raises(ImportValidationError):
        _validate(data, keys)


def test_untrusted_signer_or_missing_integrity_key_rejected(snapshot):
    data, keys = snapshot
    with pytest.raises(ImportValidationError):
        _validate(data, {})
    with pytest.raises(ImportValidationError):
        validate_snapshot(data, trusted_keys=keys, account_integrity_key="")


@pytest.mark.parametrize("text", ['{"redis":{},"redis":{}}', '{"value":NaN}'])
def test_strict_json_rejects_duplicate_keys_and_nonfinite_values(tmp_path, text):
    path = tmp_path / "snapshot.json"
    path.write_text(text)
    with pytest.raises(ImportValidationError):
        read_snapshot(path)


def test_invalid_snapshot_never_opens_database(snapshot, monkeypatch):
    import psycopg

    data, keys = snapshot
    data["redis"]["accounts"][0]["record"]["enabled"] = "0"

    def unexpected_connect(*args, **kwargs):
        pytest.fail("database accessed before validation")

    monkeypatch.setattr(psycopg, "connect", unexpected_connect)
    with pytest.raises(ImportValidationError):
        import_snapshot(data, "unused", trusted_keys=keys, account_integrity_key=INTEGRITY_KEY)


@pytest.fixture
def dedicated_database():
    """Never default to a local or live database: the parent provisions this target."""
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    from nomos.store.migrate import run_migrations

    dsn = os.environ.get("ASSURANCE_IMPORT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("requires an explicitly provisioned disposable importer database")
    target = conninfo_to_dict(dsn)
    assert target.get("dbname", "").startswith("assurance_import_test") or (
        target.get("dbname") == "nomos_test"
        and target.get("host") == "127.0.0.1"
        and target.get("port") == "55439"
    )
    run_migrations(dsn)
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "TRUNCATE ledger_entries, ledger_chains, token_index, account_permissions, "
            "account_councils, accounts, writs, writ_outcomes, intents, "
            "trusted_executors, council_workflows CASCADE"
        )
    return dsn


def test_postgres_roundtrip_and_repeat_refusal(snapshot, dedicated_database):
    import asyncio

    import psycopg
    from psycopg_pool import ConnectionPool

    from nomos.level_1.ledger.postgres_storage import PostgresLedgerStorage
    from nomos.store.postgres_store import PostgresStore

    data, keys = snapshot
    report = import_snapshot(
        data,
        dedicated_database,
        trusted_keys=keys,
        account_integrity_key=INTEGRITY_KEY,
    )
    assert report["ledger_entries"] == 2
    with ConnectionPool(dedicated_database) as pool:
        storage = PostgresLedgerStorage(pool, "ledger-legacy")
        assert [entry.to_dict() for entry in storage.iterate()] == data["ledger"]["entries"]
        assert storage.get_head().to_dict() == data["ledger"]["entries"][-1]

    async def check_durable():
        store = PostgresStore(dedicated_database, account_integrity_key=INTEGRITY_KEY)
        await store.connect()
        try:
            account = await store.get_account("agent-1")
            assert account == data["redis"]["accounts"][0]["record"]
            assert verify_account_record(
                key=INTEGRITY_KEY,
                agent_id="agent-1",
                account=account,
                permissions=await store.get_account_permissions("agent-1"),
                councils=await store.get_account_councils("agent-1"),
            )
            assert await store.get_writ("writ-1") == data["redis"]["writs"]["writ-1"]
            assert await store.get_intent("intent-1") == data["redis"]["intents"]["intent-1"]
            assert [row["intent_id"] for row in await store.get_agent_intents("agent-1")] == [
                "intent-1"
            ]
            assert (
                await store.get_writ_outcome("writ-1") == data["redis"]["writ_outcomes"]["writ-1"]
            )
            executor = data["redis"]["trusted_executors"][0]
            observed = await store.get_trusted_executor(executor["key_fingerprint"])
            assert json.loads(observed.pop("metadata_json")) == json.loads(
                executor["metadata_json"]
            )
            assert observed == {k: v for k, v in executor.items() if k != "metadata_json"}
        finally:
            await store.close()

    asyncio.run(check_durable())
    with pytest.raises(ImportValidationError, match="empty"):
        import_snapshot(
            data, dedicated_database, trusted_keys=keys, account_integrity_key=INTEGRITY_KEY
        )
    with psycopg.connect(dedicated_database) as conn:
        assert conn.execute("SELECT count(*) FROM ledger_entries").fetchone()[0] == 2


def test_postgres_failure_rolls_back_entire_import(snapshot, dedicated_database):
    import psycopg

    data, keys = snapshot
    with psycopg.connect(dedicated_database) as conn:
        conn.execute("ALTER TABLE writs ADD CONSTRAINT importer_test_failure CHECK (false)")
    try:
        with pytest.raises(psycopg.errors.CheckViolation):
            import_snapshot(
                data, dedicated_database, trusted_keys=keys, account_integrity_key=INTEGRITY_KEY
            )
        with psycopg.connect(dedicated_database) as conn:
            assert conn.execute("SELECT count(*) FROM accounts").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM ledger_entries").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM ledger_chains").fetchone()[0] == 0
    finally:
        with psycopg.connect(dedicated_database) as conn:
            conn.execute("ALTER TABLE writs DROP CONSTRAINT importer_test_failure")


def test_existing_workflow_without_ledger_still_blocks_import(snapshot, dedicated_database):
    import psycopg

    data, keys = snapshot
    with psycopg.connect(dedicated_database) as conn:
        conn.execute(
            "INSERT INTO council_workflows (council_id,intent_id,state,workflow_json) "
            "VALUES ('legacy','already-running','pending','{}')"
        )
    with pytest.raises(ImportValidationError, match="empty"):
        import_snapshot(
            data, dedicated_database, trusted_keys=keys, account_integrity_key=INTEGRITY_KEY
        )
    with psycopg.connect(dedicated_database) as conn:
        assert conn.execute("SELECT count(*) FROM ledger_entries").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM council_workflows").fetchone()[0] == 1


def test_postgres_preserves_full_recent_queue_order(snapshot, dedicated_database):
    import asyncio

    from nomos.store.postgres_store import PostgresStore

    data, keys = snapshot
    # Lexicographic ID order and old record timestamps disagree with the actual
    # Redis queue; the queue is authoritative for the newest thirty results.
    ordered = [f"intent-{i:02}" for i in range(30)]
    data["redis"]["intents"] = {
        identifier: {"intent_id": identifier, "agent_id": "agent-1", "decided_at": index}
        for index, identifier in enumerate([*ordered, "unqueued-future"])
    }
    data["redis"]["agent_intents"] = {"agent-1": ordered}
    import_snapshot(
        data, dedicated_database, trusted_keys=keys, account_integrity_key=INTEGRITY_KEY
    )

    async def check_order():
        store = PostgresStore(dedicated_database)
        await store.connect()
        try:
            observed = await store.get_agent_intents("agent-1")
            assert [record["intent_id"] for record in observed] == ordered
            await store.store_intent(
                "genuinely-new", {"intent_id": "genuinely-new", "agent_id": "agent-1"}
            )
            observed = await store.get_agent_intents("agent-1")
            assert [record["intent_id"] for record in observed] == ["genuinely-new", *ordered[:29]]
        finally:
            await store.close()

    asyncio.run(check_order())


@pytest.mark.asyncio
async def test_exporter_reads_raw_durable_state_without_mutation(snapshot, tmp_path):
    from nomos.config.settings import RedisConfig
    from nomos.store.redis_store import RedisStore
    from scripts.assurance_snapshot_legacy import export_snapshot

    data, keys = snapshot
    state = data["redis"]
    records = {
        "nomos:accounts:agent-1": ("hash", state["accounts"][0]["record"]),
        "nomos:accounts:agent-1:perms": ("set", {"read"}),
        "nomos:accounts:agent-1:councils": ("set", {"legacy"}),
        "nomos:councils:legacy:agents": ("set", {"agent-1"}),
        "nomos:token_index:test-token-hash": ("string", "agent-1"),
        "nomos:agent_intents:agent-1": ("list", ["intent-1"]),
        "nomos:trusted_executors": ("set", {state["trusted_executors"][0]["key_fingerprint"]}),
    }
    for identifier, writ in state["writs"].items():
        records[f"nomos:writs:{identifier}"] = ("string", json.dumps(writ))
    for identifier, outcome in state["writ_outcomes"].items():
        records[f"nomos:writs:{identifier}:outcome"] = ("hash", outcome)
    for identifier, intent in state["intents"].items():
        records[f"nomos:intents:{identifier}"] = ("string", json.dumps(intent))
    for executor in state["trusted_executors"]:
        records[f"nomos:trusted_executors:{executor['key_fingerprint']}"] = ("hash", executor)

    class ReadOnlyRedis:
        async def scan_iter(self, match):
            for key in sorted(records):
                if fnmatch.fnmatchcase(key, match):
                    yield key

        async def type(self, key):
            return records.get(key, ("none", None))[0]

        async def hgetall(self, key):
            return copy.deepcopy(records.get(key, (None, {}))[1])

        async def smembers(self, key):
            return set(records.get(key, (None, set()))[1])

        async def get(self, key):
            return records.get(key, (None, None))[1]

        async def lrange(self, key, start, stop):
            assert start == 0 and stop == -1
            return list(records.get(key, (None, []))[1])

    store = RedisStore(RedisConfig())
    store._client = ReadOnlyRedis()
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("".join(json.dumps(entry) + "\n" for entry in data["ledger"]["entries"]))
    exported = await export_snapshot(
        store, prefix="nomos:", council_id="legacy", ledger_path=ledger
    )
    assert exported == data
    _validate(exported, keys)

    records["nomos:councils:legacy:agents"] = ("set", {"another-agent"})
    with pytest.raises(ImportValidationError):
        await export_snapshot(store, prefix="nomos:", council_id="legacy", ledger_path=ledger)
    records["nomos:councils:legacy:agents"] = ("set", {"agent-1"})

    records["nomos:writs:corrupt"] = ("set", {"unsupported"})
    with pytest.raises(ImportValidationError):
        await export_snapshot(store, prefix="nomos:", council_id="legacy", ledger_path=ledger)
