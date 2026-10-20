# SPDX-License-Identifier: Apache-2.0
"""Durable stream isolation, retry, cadence and evidence-window contracts."""

import asyncio

import pytest

from assurance.monitor import Store
from assurance.scheduler import StreamWorker, public_run


def seed(store, count):
    seq, _ = store.checkpoint()
    store.append(
        [
            {
                "sequence": i,
                "content_hash": f"hash-{i}",
                "entry_type": "policy_updated",
                "timestamp": "2026-10-01T00:00:00+00:00",
                "payload": {},
            }
            for i in range(seq + 1, count)
        ],
        store.checkpoint(),
    )


@pytest.mark.asyncio
async def test_durable_windows_catch_up_and_cadence_without_gaps(tmp_path):
    store = Store(tmp_path / "db", "chain")
    seed(store, 2401)
    observed = []

    async def analyze(events, chain):
        observed.append([e["sequence"] for e in events])
        return [{"id": "stable", "kind": "example", "evidence": []}], {}

    worker = StreamWorker(store, "tool-issues", analyze, config={"version": 1})
    while worker.progress() < 2400:
        await worker.step(now=100)
    assert worker.progress() == 2400
    assert observed[-1][-1] == 2400
    assert len(observed[-1]) == 2000
    window = store.stream_run("tool-issues", worker.config_digest)["coverage"]["evidence_window"]
    assert window["earlier_context_omitted"] is True
    assert window["event_count"] == window["event_limit"] == 2000
    assert observed[1][0] <= observed[0][-1]
    assert store.query_findings()[1] == 1
    seed(store, 2402)
    assert await worker.step(now=101) is None
    resumed = StreamWorker(
        Store(store.path, "chain"), "tool-issues", analyze, config={"version": 1}
    )
    await resumed.step(now=401)
    assert resumed.progress() == 2401
    assert store.analysis_sequence() == 2401


@pytest.mark.asyncio
async def test_failure_isolation_bounded_retry_and_same_snapshot(tmp_path):
    store = Store(tmp_path / "db", "chain")
    seed(store, 3)
    attempts = []

    async def fail(events, chain):
        attempts.append([e["sequence"] for e in events])
        raise RuntimeError("secret upstream payload must not be retained")

    async def ok(events, chain):
        return [], {}

    bad = StreamWorker(store, "ethos-divergence", fail, config={}, max_attempts=2)
    good = StreamWorker(store, "tool-issues", ok, config={})
    first = await bad.step(now=1)
    assert first["status"] == "failed"
    assert bad.progress() == -1
    seed(store, 4)
    await good.step(now=1)
    assert good.progress() == 3
    await bad.step(now=10)
    await bad.step(now=100)
    assert len(attempts) == 2 and attempts[0] == attempts[1] == [0, 1, 2]
    assert "secret upstream" not in str(store.runs())
    assert bad.progress() == -1
    bad.retry(store.run(first["id"]))
    await bad.step(now=200)
    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_cancelled_work_resumes_and_public_results_omit_raw_snapshots(tmp_path):
    store = Store(tmp_path / "db", "chain")
    seed(store, 1)

    async def cancel(events, chain):
        raise asyncio.CancelledError

    worker = StreamWorker(
        store, "tool-issues", cancel, config={}, artifacts={"documents": "private"}
    )
    with pytest.raises(asyncio.CancelledError):
        await worker.step()
    run = store.runs()[0]
    assert run["status"] == "pending"
    assert "input_snapshot" not in public_run(run)
    assert "ethos_artifacts" not in public_run(run)
    assert store.progress("tool-issues", worker.config_digest) == -1


@pytest.mark.asyncio
async def test_missing_context_commits_explicit_abstention_and_versions_reanalyze(tmp_path):
    store = Store(tmp_path / "db", "chain")
    seed(store, 1)

    async def abstain(events, chain):
        return [], {"status": "insufficient_evidence"}

    old = StreamWorker(store, "ethos-divergence", abstain, config={"ethos": "v1"})
    first = await old.step()
    new = StreamWorker(store, "ethos-divergence", abstain, config={"ethos": "v2"})
    second = await new.step()
    assert first["status"] == second["status"] == "insufficient_evidence"
    assert first["id"] != second["id"]
    assert old.progress() == new.progress() == 0
    assert store.count_runs() == 2


@pytest.mark.asyncio
async def test_pending_snapshot_restores_metadata_and_timeout_usage_is_unknown(tmp_path):
    store = Store(tmp_path / "db", "chain")
    seed(store, 1)

    async def slow(events, chain):
        await asyncio.sleep(1)
        return [], {}

    worker = StreamWorker(
        store, "ethos-divergence", slow, config={"model": "approved"}, timeout=0.001
    )
    pending = store.make_run("ethos-divergence", worker.config_digest, artifacts={})
    assert "configuration" not in pending
    result = await worker.step()
    assert result["configuration"] == {"model": "approved"}
    assert result["agents"] == ["unattributed"]
    assert result["status"] == "failed"
    assert result["coverage"]["model_requests"] is None
    assert result["coverage"]["usage_status"] == "unavailable_after_timeout"
    assert worker.progress() == -1


@pytest.mark.asyncio
async def test_changed_configuration_labels_reanalysis_and_links_run_supersession(tmp_path):
    store = Store(tmp_path / "db", "chain")
    seed(store, 2)

    async def with_finding(events, chain):
        return [{"id": "stable", "kind": "example", "evidence": [], "disposition": "advisory"}], {}

    async def empty(events, chain):
        return [], {}

    first = await StreamWorker(store, "tool-issues", with_finding, config={"detector": 1}).step()
    second = await StreamWorker(store, "tool-issues", empty, config={"detector": 2}).step()
    assert first["reanalysis"] is None
    assert second["reanalysis"]["reason"] == "configuration_changed"
    assert second["reanalysis"]["previous_run_ids"] == [first["id"]]
    assert second["reanalysis"]["ethos_policy"] == "historically_applicable_document_versions"
    assert second["supersedes_run_ids"] == [first["id"]]
    assert store.run(first["id"])["superseded_by_run_ids"] == [second["id"]]
    # A newer successful analysis supersedes a run, never deletes or declares
    # the old finding cleared solely because the newer model did not return it.
    assert store.get_finding("stable")["disposition"] == "advisory"
    assert store.get_finding("stable")["history"] == [first["id"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("new_status", ["failed", "insufficient_evidence", "not_configured"])
async def test_failed_or_abstained_reanalysis_never_supersedes_complete_result(
    tmp_path, new_status
):
    store = Store(tmp_path / "db", "chain")
    seed(store, 1)

    async def first_result(events, chain):
        return [{"id": "original", "kind": "example", "evidence": []}], {}

    async def next_result(events, chain):
        if new_status == "failed":
            raise RuntimeError("inference outage")
        return [], {"status": new_status}

    first = await StreamWorker(store, "tool-issues", first_result, config={"v": 1}).step()
    second = await StreamWorker(store, "tool-issues", next_result, config={"v": 2}).step()
    assert second["reanalysis"] is not None
    assert second["supersedes_run_ids"] == []
    assert store.run(first["id"])["superseded_by_run_ids"] == []
    assert store.get_finding("original") is not None


@pytest.mark.asyncio
async def test_different_evidence_is_not_a_superseding_assessment(tmp_path):
    store = Store(tmp_path / "db", "chain")
    seed(store, 1)

    async def analysis(events, chain):
        return [], {}

    first = await StreamWorker(store, "tool-issues", analysis, config={"v": 1}).step()
    seed(store, 2)
    second = await StreamWorker(store, "tool-issues", analysis, config={"v": 2}).step()
    assert second["reanalysis"] is not None
    assert second["evidence_digest"] != first["evidence_digest"]
    assert second["supersedes_run_ids"] == []


@pytest.mark.asyncio
async def test_ethos_reanalysis_preserves_contract_artifacts_and_partial_scope_history(tmp_path):
    store = Store(tmp_path / "db", "chain")
    seed(store, 1)
    requirement = {
        "agent_id": "a",
        "document_id": "deployment",
        "version": "1",
        "sha256": "hash",
        "requirement_id": "REQUEST-01",
        "eligible_traces": 1,
        "excluded_traces": 0,
        "required_evidence": ["request"],
        "status": "complete",
    }

    async def complete(events, chain):
        return [{"id": "stable", "kind": "ethos_divergence", "evidence": []}], {
            "requirements": [requirement]
        }

    async def partial(events, chain):
        return [], {
            "requirements": [
                requirement,
                {
                    **requirement,
                    "requirement_id": "EXECUTION-01",
                    "status": "insufficient_evidence",
                },
            ]
        }

    old_artifacts = {
        "manifest": {"document_id": "deployment", "version": "1"},
        "documents": {"ETHOS.md": "old contract"},
    }
    first = await StreamWorker(
        store, "ethos-divergence", complete, config={"model": "old"}, artifacts=old_artifacts
    ).step()
    second = await StreamWorker(
        store,
        "ethos-divergence",
        complete,
        config={"model": "new"},
        artifacts={"documents": "new snapshot"},
    ).step()
    assert second["supersedes_run_ids"] == [first["id"]]
    assert store.run(first["id"])["ethos_artifacts"] == old_artifacts
    assert set(store.get_finding("stable")["history"]) == {first["id"], second["id"]}
    third = await StreamWorker(
        store, "ethos-divergence", partial, config={"model": "partial"}
    ).step()
    assert third["status"] == "complete"  # Individual requirement abstentions remain explicit.
    assert third["supersedes_run_ids"] == []
    assert store.run(second["id"])["superseded_by_run_ids"] == []


@pytest.mark.asyncio
async def test_attempt_usage_preserves_partial_failure_and_unknown_timeout(tmp_path):
    store = Store(tmp_path / "db", "chain")
    seed(store, 1)
    calls = 0

    class FailedInferenceError(RuntimeError):
        def __init__(self):
            self.coverage = {"model_requests": 2, "cached_requirement_checks": 0}

    async def analysis(events, chain):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise FailedInferenceError()
        return [], {"model_requests": 1, "cached_requirement_checks": 1}

    worker = StreamWorker(store, "ethos-divergence", analysis, config={})
    first = await worker.step(now=1)
    running = store.run(first["id"])
    running.update(status="running", started_at="transient-start")
    store.save_run(running)
    assert len(store.run(first["id"])["inference_attempts"]) == 1
    assert store.run(first["id"])["known_model_requests"] == 2
    second = await worker.step(now=10)
    assert first["known_model_requests"] == 2
    assert second["model_requests_total"] == second["known_model_requests"] == 3
    assert len(second["inference_attempts"]) == 2
    store.save_run(store.run(second["id"]))
    assert len(store.run(second["id"])["inference_attempts"]) == 2  # Idempotent save.
    run = store.run(second["id"])
    run.update(
        status="failed",
        started_at="new-attempt",
        completed_at="timed-out",
        coverage={"model_requests": None, "usage_status": "unavailable_after_timeout"},
    )
    store.save_run(run)
    assert run["known_model_requests"] == 3
    assert run["model_requests_total"] is None
