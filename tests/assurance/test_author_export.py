# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Selection, privacy, frozen downloads and real pinned trace preparation."""

import hashlib
import io
import json
import os
import zipfile
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from assurance.analysis import reference
from assurance.app import create_app
from assurance.author_export import CaptureCache, ExportError, build_capture, resolve_selection
from assurance.monitor import Store
from tests.assurance.test_service import settings


def read_bundle(bundle):
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def record(sequence, kind="intent_review_recorded", **payload):
    return {
        "sequence": sequence,
        "entry_type": kind,
        "timestamp": "2026-10-27T00:00:00Z",
        "content_hash": "sha256:" + hashlib.sha256(str(sequence).encode()).hexdigest(),
        "payload": {
            "schema_version": 1,
            "agent_id": "test-agent",
            "council_id": "c",
            "intent_id": f"i-{sequence}",
            "intent": {"command": "deploy staging", "target_environment": "staging"},
            "final": {"decision": "deny"},
            **payload,
        },
    }


def populate(store, events=None, *, finding_id="f-1", artifacts=None, evidence=None):
    events = events or [record(0), record(1)]
    if store.checkpoint()[0] == -1:
        store.append(events, store.checkpoint())
    run = store.make_run("tool-issues", "cfg", artifacts=artifacts or {})
    run.update(status="complete", completed_at="2026-10-27T00:00:00Z")
    store.save_run(
        run,
        [
            {
                "id": finding_id,
                "kind": "repeated_unchanged_denial",
                "detector": "nomos:test",
                "agent_id": "test-agent",
                "disposition": "advisory",
                "explanation": "scope mismatch",
                "evidence": evidence or [reference(events[0], store.chain_id)],
            }
        ],
    )
    return run


def test_selection_uses_historical_snapshot_and_deduplicates_shared_evidence(tmp_path):
    store = Store(tmp_path / "db", "ledger-c")
    run = populate(store)
    with closing(store.connect()) as db, db:
        db.execute("DELETE FROM events")
        item = store.get_finding("f-1")
        item["id"] = "f-2"
        db.execute("INSERT INTO findings VALUES (?,?)", ("f-2", json.dumps(item)))
    frozen = resolve_selection(store, ["f-1", "f-2"])
    assert [e["sequence"] for e in frozen["records"]] == [0]
    assert [r["id"] for r in frozen["runs"]] == [run["id"]]
    files = read_bundle(build_capture(frozen, "council-decision", [])[0])
    assert len(json.loads(files["findings.json"])) == 2
    assert b"deploy staging" in files["evidence/records.jsonl"]
    assert b"i-1" not in files["evidence/records.jsonl"]


def test_missing_historical_record_is_reported_without_substitution(tmp_path):
    store = Store(tmp_path / "db", "ledger-c")
    populate(
        store, evidence=[{"chain_id": "ledger-c", "sequence": 7000, "content_hash": "missing"}]
    )
    frozen = resolve_selection(store, ["f-1"])
    assert frozen["records"] == []
    bundle, preview = build_capture(frozen, "client-agent", [])
    assert preview["completeness"][0]["status"] == "unsupported_for_trace_derivation"
    files = read_bundle(bundle)
    assert not any(n.startswith("traces/") for n in files)
    assert b"referenced_record_unavailable" in files["manifest.json"]


def test_explicit_execution_links_no_agent_or_timestamp_join(tmp_path):
    store = Store(tmp_path / "db", "ledger-c")
    events = [
        record(0),
        record(1),
        record(2, "execution_started", intent_id="i-0", token_id="token-0"),
        record(
            3, "execution_completed", intent_id=None, token_id="token-0", output={"success": True}
        ),
        record(4, "execution_failed", intent_id=None, token_id="other-token"),
    ]
    populate(store, events)
    assert [e["sequence"] for e in resolve_selection(store, ["f-1"])["records"]] == [0, 2, 3]


def test_redaction_covers_content_ethos_metadata_and_inventory(tmp_path):
    store = Store(tmp_path / "db", "ledger-c")
    secret = "test-sensitive-value-130"  # pragma: allowlist secret
    content = f"# Agent\nOnly staging. token={secret}\n"
    sha = hashlib.sha256(content.encode()).hexdigest()
    name = "../../unsafe-" + secret
    artifacts = {
        "manifest": {
            "documents": [
                {"document_id": secret, "version": "v1", "sha256": sha, "filename": name}
            ],
            "agents": {"test-agent": [secret + "@v1"]},
        },
        "documents": {name: content},
    }
    events = [
        record(
            0,
            intent={"command": f"deploy --api-key={secret}", "parameters": {"password": secret}},
            chairman={"explanation": secret},
        ),
        record(
            1,
            "execution_failed",
            intent_id="i-0",
            error=secret,
            output={"text": secret},
            private_extra=secret,
        ),
    ]
    populate(store, events, artifacts=artifacts)
    bundle, preview = build_capture(resolve_selection(store, ["f-1"]), "council-decision", [secret])
    files = read_bundle(bundle)
    assert all(secret.encode() not in body and secret not in name for name, body in files.items())
    manifest = json.loads(files["manifest.json"])
    assert manifest["ethos"][0]["source_sha256"] == sha
    assert manifest["ethos"][0]["transformed"] is True
    assert preview["completeness"][0]["status"] == "additional_context_required"
    assert preview["redaction_count"] > 0
    for name, item in manifest["file_inventory"].items():
        assert hashlib.sha256(files[name]).hexdigest() == item["sha256"]
    assert all(not name.startswith("/") and ".." not in name.split("/") for name in files)


def test_capture_freezes_bytes_until_expiry(tmp_path, monkeypatch):
    store = Store(tmp_path / "db", "ledger-c")
    populate(store)
    cache = CaptureCache()
    preview = cache.prepare(store, ["f-1"], "council-decision", [])
    first = cache.download(preview["capture_id"])
    store.append([record(2)], store.checkpoint())
    assert cache.download(preview["capture_id"]) == first
    monkeypatch.setattr("assurance.author_export.TTL_SECONDS", 0)
    with pytest.raises(ExportError, match="expired"):
        cache.download(preview["capture_id"])


@pytest.mark.parametrize("ids", [[], ["../file"], ["a"] * 21, ["f-1", "f-1"], ["unknown"]])
def test_selection_validation(tmp_path, ids):
    store = Store(tmp_path / "db", "ledger-c")
    populate(store)
    with pytest.raises(ExportError):
        resolve_selection(store, ids)


@pytest.mark.parametrize("enabled", [False, True])
def test_operator_only_rich_preview_download_and_retry_boundary(tmp_path, enabled):
    app = create_app(settings(tmp_path, evaluation_export_enabled=enabled), run_worker=False)
    populate(app.state.store)
    with TestClient(app) as client:
        selection = {"finding_ids": ["f-1"], "subject": "council-decision"}
        routes = [
            ("/api/evaluation-export/preview", selection),
            ("/api/evaluation-export", {"capture_id": "a" * 64}),
        ]
        for route, body in routes:
            assert client.post(route, json=body).status_code == 401
            assert (
                client.post(
                    route,
                    json=body,
                    headers={
                        "Authorization": "Bearer read-only",
                        "X-Assurance-Operator": "evaluation-export",
                    },
                ).status_code
                == 401
            )
            for headers in (
                {},
                {"X-Assurance-Operator": "retry"},
                {"X-Assurance-Operator": "evaluation-export", "Sec-Fetch-Site": "cross-site"},
                {"X-Assurance-Operator": "evaluation-export", "Sec-Fetch-Site": "same-site"},
            ):
                assert (
                    client.post(
                        route, json=body, auth=("reviewer", "password"), headers=headers
                    ).status_code
                    == 403
                )
        auth = {
            "auth": ("reviewer", "password"),
            "headers": {
                "X-Assurance-Operator": "evaluation-export",
                "Sec-Fetch-Site": "same-origin",
            },
        }
        preview = client.post(routes[0][0], json=selection, **auth)
        if not enabled:
            assert preview.status_code == 404
            assert client.post(routes[1][0], json=routes[1][1], **auth).status_code == 404
            return
        assert preview.status_code == 200
        downloaded = client.post(
            routes[1][0], json={"capture_id": preview.json()["capture_id"]}, **auth
        )
        files = read_bundle(downloaded.content)
        assert json.loads(files["manifest.json"])["capture_id"] == preview.json()["capture_id"]
        assert downloaded.headers["cache-control"] == "no-store"
        assert (
            client.get("/api/status", headers={"Authorization": "Bearer read-only"}).json()[
                "evaluation_export_enabled"
            ]
            is True
        )
        assert client.post(routes[1][0], json={"capture_id": "f" * 64}, **auth).status_code == 409
        assert (
            client.post(routes[0][0], json={**selection, "path": "/etc/passwd"}, **auth).status_code
            == 422
        )
        assert (
            client.post(
                routes[0][0], json={**selection, "finding_ids": ["a"] * 21}, **auth
            ).status_code
            == 422
        )


def client_event(sequence=0, **overrides):
    return record(
        sequence,
        "execution_failed",
        intent_id=None,
        assurance_schema=1,
        run_id="run",
        logical_case_id="case",
        tool_name="staging.lookup",
        call_id=f"call-{sequence}",
        input={"name": "test"},
        output={"error": "unavailable"},
        assurance_context={
            "schema_version": 1,
            "conversation_complete": True,
            "messages_position": "before_tools",
            "messages": [{"role": "user", "content": "Look up the staging test service status."}],
        },
        **overrides,
    )


def test_client_trace_passes_real_pinned_eval_author(tmp_path, monkeypatch):
    root = Path(os.environ.get("EVAL_AUTHOR_ROOT", "/opt/eval-author"))
    if not (root / "skills/eval-author-trace-environment/scripts/trace_environment.py").is_file():
        pytest.skip("pinned Eval Author trace helper required")
    monkeypatch.setenv("EVAL_AUTHOR_ROOT", str(root))
    store = Store(tmp_path / "db", "ledger-c")
    events = [client_event(0), client_event(1)]
    populate(store, events)
    bundle, preview = build_capture(resolve_selection(store, ["f-1"]), "client-agent", [])
    files = read_bundle(bundle)
    trace = json.loads(files[preview["completeness"][0]["trace"]])
    assert (
        trace["steps"][0]["message"]
        == events[0]["payload"]["assurance_context"]["messages"][0]["content"]
    )
    assert [s["tool_calls"][0]["tool_call_id"] for s in trace["steps"][1:]] == ["call-0", "call-1"]
    assert json.loads(files["validation.json"])["traces"][0]["valid"] is True
    assert trace["steps"][1]["extra"]["source"]["sequence"] == 0
    assert trace["agent"]["version"] == "unrecorded"
    assert trace["agent"]["extra"]["version_recorded"] is False


@pytest.mark.parametrize("missing", ["messages", "output", "input", "call_id"])
def test_incomplete_client_never_acquires_invented_evidence(tmp_path, missing):
    store = Store(tmp_path / "db", "ledger-c")
    event = client_event()
    if missing == "messages":
        del event["payload"]["assurance_context"]["messages"]
    else:
        del event["payload"][missing]
    populate(store, [event])
    bundle, preview = build_capture(resolve_selection(store, ["f-1"]), "client-agent", [])
    assert preview["completeness"][0]["status"] == "unsupported_for_trace_derivation"
    assert not any(n.startswith("traces/") for n in read_bundle(bundle))


def test_tool_finding_resolves_matching_historical_ethos_run_and_optional_context(tmp_path):
    store = Store(tmp_path / "db", "ledger-c")
    event = record(
        0,
        assurance_context={
            "schema_version": 1,
            "tool_catalog": [{"name": "staging.lookup"}],
            "environment": {"version": "test-v1"},
        },
    )
    populate(store, [event])
    content = "# Staging agent\nOnly staging changes are allowed.\n"
    sha = hashlib.sha256(content.encode()).hexdigest()
    name = f"ethos/pilot/v1/{sha}/ETHOS.md"
    artifacts = {
        "manifest": {
            "documents": [
                {"document_id": "pilot", "version": "v1", "sha256": sha, "filename": name}
            ],
            "agents": {"test-agent": ["pilot@v1"]},
        },
        "documents": {name: content},
    }
    context_run = store.make_run("ethos-divergence", "ethos-cfg", artifacts=artifacts)
    context_run.update(status="complete")
    store.save_run(context_run, [])
    frozen = resolve_selection(store, ["f-1"])
    assert len(frozen["runs"]) == 2
    bundle, _ = build_capture(frozen, "council-decision", [])
    files = read_bundle(bundle)
    assert "context/tools.json" in files and "context/environment.json" in files
    assert any(name.endswith("/ETHOS.md") for name in files)
    manifest = json.loads(files["manifest.json"])
    assert manifest["ethos"][0]["source_sha256"] == sha
    assert manifest["ethos"][0]["applicable_records"] == [reference(event, store.chain_id)]


def test_shared_receipts_are_one_identity_with_multiple_source_references(tmp_path):
    store = Store(tmp_path / "db", "ledger-c")
    receipt = {
        "writ_id": "receipt-1",
        "command": "/usr/bin/false",
        "success": False,
        "exit_code": 1,
    }
    events = [
        record(i, intent={"command": "review", "metadata": {"certified_evidence": [receipt]}})
        for i in range(2)
    ]
    populate(store, events, evidence=[reference(e, store.chain_id) for e in events])
    bundle, _ = build_capture(resolve_selection(store, ["f-1"]), "council-decision", [])
    files = read_bundle(bundle)
    summaries = json.loads(files["context/receipt-summaries.json"])
    assert len(summaries) == 1 and len(summaries[0]["sources"]) == 2
    assert len(summaries[0]["variants"]) == 1
    assert summaries[0]["conflicting_summaries"] is False


@pytest.mark.parametrize(
    "mutation", ["result-mismatch", "duplicate-call", "ambiguous-dialogue", "nested"]
)
def test_ambiguous_client_evidence_stays_outside_validated_traces(tmp_path, mutation):
    store = Store(tmp_path / "db", "ledger-c")
    events = [client_event(0), client_event(1)]
    if mutation == "result-mismatch":
        events[0]["payload"]["result_id"] = "foreign-call"
    elif mutation == "duplicate-call":
        events[1]["payload"]["call_id"] = "call-0"
    elif mutation == "ambiguous-dialogue":
        for event in events:
            del event["payload"]["assurance_context"]["messages_position"]
    else:
        events[0]["payload"]["assurance_context"]["children"] = [{"id": "child"}]
    populate(store, events)
    bundle, preview = build_capture(resolve_selection(store, ["f-1"]), "client-agent", [])
    assert preview["completeness"][0]["status"] == "unsupported_for_trace_derivation"
    assert not any(name.startswith("traces/") for name in read_bundle(bundle))


def test_capability_and_authorization_secrets_never_leave_content_or_logs(tmp_path, caplog):
    store = Store(tmp_path / "db", "ledger-c")
    event = record(
        0,
        intent={
            "command": "Authorization: Basic dXNlcjpwYXNzd29yZA==",
            "parameters": {"auth_token": "reusable-value", "credentials": "more-private"},
        },
        output={"error": "Bearer private-value"},
    )
    populate(store, [event])
    bundle, _ = build_capture(resolve_selection(store, ["f-1"]), "council-decision", [])
    files = read_bundle(bundle)
    for secret in ("dXNlcjpwYXNzd29yZA==", "reusable-value", "more-private", "private-value"):
        assert all(secret.encode() not in body for body in files.values())
        assert secret not in caplog.text


def test_capture_bounds_omit_extra_related_records_and_downgrade_completeness(
    tmp_path, monkeypatch
):
    store = Store(tmp_path / "db", "ledger-c")
    events = [record(i, intent_id="shared-intent") for i in range(4)]
    populate(store, events)
    monkeypatch.setattr("assurance.author_export.MAX_RECORDS", 2)
    frozen = resolve_selection(store, ["f-1"])
    assert len(frozen["records"]) == 2
    assert frozen["omissions"]
    _, preview = build_capture(frozen, "council-decision", [])
    assert preview["completeness"][0]["status"] == "additional_context_required"


@pytest.mark.asyncio
async def test_native_execution_keeps_ethos_at_execution_time(tmp_path, monkeypatch):
    from datetime import datetime
    from types import SimpleNamespace

    from assurance.ethos import EthosRegistry, InferenceConfig, analyze_ethos
    from assurance.monitor import verify_batch
    from nomos.level_0.clock import MockClock
    from nomos.level_0.crypto import KeyPair, Signer
    from nomos.level_1.ledger.chain import LedgerChain
    from nomos.level_1.ledger.entry import LedgerEntryType

    clock = MockClock(datetime.fromisoformat("2026-10-10T00:00:00+00:00"))
    key = KeyPair.generate("test-signer")
    chain = LedgerChain(signer=Signer(key), clock=clock, chain_id="ledger-c")
    chain.initialize("tests")
    payload = record(1)["payload"]
    payload["intent_id"] = "intent-1"
    payload["intent"].update(agent_id="test-agent", council_id="c", intent_id="intent-1")
    chain.append(LedgerEntryType.INTENT_REVIEW_RECORDED, payload)
    clock.set(datetime.fromisoformat("2026-10-21T00:00:00+00:00"))
    chain.append(
        LedgerEntryType.EXECUTION_STARTED,
        {"intent_id": "intent-1", "token_id": "token-1", "action": {"command": "deploy staging"}},
    )
    chain.append(
        LedgerEntryType.EXECUTION_COMPLETED,
        {"token_id": "token-1", "output": {"success": True, "target_environment": "production"}},
    )
    keys = {key.key_id: key.public_key_hex()}
    events = verify_batch(
        [chain.get(i).to_dict() for i in range(chain.length())], -1, None, chain.chain_id, keys
    )
    documents = []
    for version, start, end, text in [
        ("1", "2026-10-01T00:00:00Z", "2026-10-20T00:00:00Z", "Execute staging actions only."),
        ("2", "2026-10-20T00:00:00Z", None, "Execute staging actions only; never production."),
    ]:
        path = tmp_path / f"v{version}.md"
        path.write_text(f"# EXECUTE-01\n{text}\n")
        documents.append(
            {
                "id": "pilot",
                "version": version,
                "path": path.name,
                "effective_from": start,
                "effective_until": end,
                "requirements": [{"id": "EXECUTE-01", "text": text, "evidence": ["execution"]}],
            }
        )
    mapping = tmp_path / "mapping.json"
    mapping.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "documents": documents,
                "agents": {"test-agent": ["pilot@1", "pilot@2"]},
            }
        )
    )
    registry = EthosRegistry.from_file(mapping)

    async def detect(snapshot, text, *_):
        assert "version 2" in text
        trace = next(iter(snapshot))
        assert trace.attributes["agent_id"] == "test-agent"
        return SimpleNamespace(
            problems=[
                SimpleNamespace(
                    description="Recorded production execution violates the execution contract.",
                    supporting_trace_ids=(trace.id,),
                    candidate_trace_ids=(),
                )
            ]
        )

    monkeypatch.setattr("assurance.ethos._detect", detect)
    findings, _ = await analyze_ethos(
        events,
        chain.chain_id,
        registry,
        InferenceConfig(
            model="openai/fixture",
            api_base="https://fixture.example/v1",
            api_key="fixture-key",  # pragma: allowlist secret
        ),
    )
    assert len(findings) == 1 and findings[0]["ethos"]["version"] == "2"
    assert findings[0]["evidence"][0]["sequence"] == 3
    app = create_app(
        settings(tmp_path, evaluation_export_enabled=True).model_copy(
            update={"chain_id": chain.chain_id, "keys": keys}
        ),
        run_worker=False,
    )
    store = app.state.store
    store.append(events, store.checkpoint())
    artifacts = {
        "manifest": registry.manifest(),
        "documents": {name: body.decode() for name, body in registry.export_documents().items()},
    }
    run = store.make_run("ethos-divergence", "cfg", artifacts=artifacts)
    run.update(status="complete", completed_at="2026-10-21T00:00:00Z")
    store.save_run(run, findings)
    with TestClient(app) as client:
        auth = {
            "auth": ("reviewer", "password"),
            "headers": {"X-Assurance-Operator": "evaluation-export"},
        }
        preview = client.post(
            "/api/evaluation-export/preview",
            json={"finding_ids": [findings[0]["id"]], "subject": "council-decision"},
            **auth,
        )
        assert preview.status_code == 200
        downloaded = client.post(
            "/api/evaluation-export", json={"capture_id": preview.json()["capture_id"]}, **auth
        )
    files = read_bundle(downloaded.content)
    contracts = json.loads(files["manifest.json"])["ethos"]
    by_version = {doc["version"]: doc for doc in contracts}
    assert set(by_version) == {"1", "2"}
    assert [ref["sequence"] for ref in by_version["1"]["applicable_records"]] == [1]
    assert [ref["sequence"] for ref in by_version["2"]["applicable_records"]] == [3]
    assert "never production" in files[by_version["2"]["path"]].decode()

    # A request contract alone cannot establish availability of the finding's
    # execution contract when that historical version is no longer retained.
    frozen = resolve_selection(store, [findings[0]["id"]])
    frozen["runs"][0]["ethos_artifacts"]["manifest"]["documents"] = [
        d
        for d in frozen["runs"][0]["ethos_artifacts"]["manifest"]["documents"]
        if d["version"] == "1"
    ]
    _, missing = build_capture(frozen, "council-decision", [])
    assert missing["completeness"][0]["status"] == "additional_context_required"
    assert any(
        "contract/ETHOS" in reason for reason in missing["completeness"][0]["missing_inputs"]
    )
