# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Immutable contract handoff; export validation is separate from analysis."""

import hashlib
import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from assurance.export import build_bundle


def fixture_store(artifacts=None):
    return SimpleNamespace(
        chain_id="chain",
        events=lambda **_: [],
        findings=lambda **_: [],
        checkpoint=lambda: (-1, None),
        analysis_sequence=lambda: -1,
        runs=lambda **_: [
            {
                "id": "run-1",
                "stream": "ethos",
                "status": "completed",
                "coverage": {"requirements": []},
                "ethos_artifacts": artifacts,
                "input_snapshot": [{"payload": "must-never-export"}],
            }
        ],
        count_runs=lambda **_: 1,
    )


@pytest.fixture
def validator(tmp_path, monkeypatch):
    path = tmp_path / "skills/eval-author-audit/scripts/audit_spec/validate.py"
    path.parent.mkdir(parents=True)
    path.write_text('import json; print(json.dumps({"valid": True}))\n')
    monkeypatch.setenv("EVAL_AUTHOR_ROOT", str(tmp_path))
    return path


def test_export_uses_exact_immutable_run_documents(validator):
    content = "# Deployment agent\n\nNever deploy to production.\n"
    digest = hashlib.sha256(content.encode()).hexdigest()
    name = f"ethos/deployment/v1/{digest}/ETHOS.md"
    manifest = {
        "documents": [
            {"document_id": "deployment", "version": "v1", "sha256": digest, "filename": name}
        ],
        "agents": {"deploy-1": ["deployment@v1"]},
    }
    artifacts = {"manifest": manifest, "documents": {name: content}}
    with zipfile.ZipFile(io.BytesIO(build_bundle(fixture_store(artifacts)))) as bundle:
        assert bundle.read(name).decode() == content
        assert "ETHOS.md" not in bundle.namelist()
        exported = json.loads(bundle.read("manifest.json"))
        assert exported["ethos_registries"][0]["manifest"] == manifest
        assert json.loads(bundle.read("runs.json"))[0]["id"] == "run-1"
        assert "ethos_artifacts" not in json.loads(bundle.read("runs.json"))[0]
        assert not any(b"must-never-export" in bundle.read(name) for name in bundle.namelist())
        assert name in bundle.read("audit.md").decode()
        assert json.loads(bundle.read("validation.json"))["valid"] is True


def test_export_without_configured_ethos_does_not_invent_contract(validator):
    with zipfile.ZipFile(io.BytesIO(build_bundle(fixture_store()))) as bundle:
        assert not any(name.endswith("ETHOS.md") for name in bundle.namelist())
        assert json.loads(bundle.read("manifest.json"))["ethos_registries"] == []
        assert "findings.json" in bundle.read("audit.md").decode()


@pytest.mark.parametrize("name,digest", [("../ETHOS.md", "a" * 64), ("ethos/ETHOS.md", "0" * 64)])
def test_export_rejects_unsafe_or_changed_snapshots(validator, name, digest):
    artifacts = {
        "manifest": {"documents": [{"filename": name, "sha256": digest}]},
        "documents": {name: "changed"},
    }
    with pytest.raises(RuntimeError, match="ETHOS"):
        build_bundle(fixture_store(artifacts))


def test_export_requires_validator_installation(tmp_path, monkeypatch):
    monkeypatch.setenv("EVAL_AUTHOR_ROOT", str(tmp_path))
    with pytest.raises(RuntimeError, match="validator unavailable"):
        build_bundle(fixture_store())


def test_export_rejects_invalid_validator_result(validator):
    validator.write_text("print('{\"valid\": false}')\n")
    with pytest.raises(RuntimeError, match="did not confirm"):
        build_bundle(fixture_store())


def test_trace_export_omits_content_and_private_attributes(validator, monkeypatch):
    trace = SimpleNamespace(id="t1", root_spans=[SimpleNamespace(id="s1", input="secret")])
    monkeypatch.setattr("assurance.export.project_traces", lambda *_: ([trace], {}))
    with zipfile.ZipFile(io.BytesIO(build_bundle(fixture_store()))) as bundle:
        row = json.loads(bundle.read("traces.redacted.jsonl"))
        assert row["trace_id"] == "t1"
        assert row["spans"] == [{"span_id": "s1"}]
        assert b"secret" not in bundle.read("traces.redacted.jsonl")


def test_export_does_not_leak_model_quoted_trace_content(validator):
    store = fixture_store()
    store.findings = lambda: [
        {
            "id": "f",
            "kind": "ethos_divergence",
            "evidence": [],
            "explanation": "The command was secret-unexportable",
        }
    ]
    with zipfile.ZipFile(io.BytesIO(build_bundle(store))) as bundle:
        assert not any(b"secret-unexportable" in bundle.read(name) for name in bundle.namelist())


def test_run_export_allowlists_provenance_metadata(validator):
    store = fixture_store()
    original = store.runs()[0]
    original.update(
        configuration={"endpoint": "must-never-export"},
        agents=["must-never-export"],
        coverage={"detail": "must-never-export"},
        error_type="must-never-export",
        future_field="must-never-export",
        config_digest="cfg",
        attempts=2,
        sequence_end=10,
    )
    store.runs = lambda **_: [original]
    with zipfile.ZipFile(io.BytesIO(build_bundle(store))) as bundle:
        (run,) = json.loads(bundle.read("runs.json"))
        assert run == {
            "id": "run-1",
            "stream": "ethos",
            "status": "completed",
            "config_digest": "cfg",
            "attempts": 2,
            "sequence_end": 10,
        }
        assert not any(b"must-never-export" in bundle.read(name) for name in bundle.namelist())


@pytest.mark.parametrize("malformation", ["conflict", "orphans"])
def test_export_preserves_other_agent_when_projection_rejects_record(validator, malformation):
    from copy import deepcopy

    store = fixture_store()

    def event(sequence, agent):
        return {
            "sequence": sequence,
            "content_hash": f"hash-{sequence}",
            "timestamp": "2026-10-10T00:00:00Z",
            "entry_type": "execution_failed",
            "payload": {
                "assurance_schema": 1,
                "agent_id": agent,
                "run_id": "run",
                "logical_case_id": "case",
                "tool_name": "lookup",
                "call_id": "call",
            },
        }

    first = event(0, "other")
    broken = deepcopy(first)
    broken.update(sequence=1, content_hash="hash-1")
    if malformation == "conflict":
        broken["payload"]["logical_case_id"] = "other-case"
    else:
        broken["payload"]["assurance_context"] = {"schema_version": 1, "orphan_results": "bad"}
    store.events = lambda **_: [first, broken, event(2, "good-agent")]
    with zipfile.ZipFile(io.BytesIO(build_bundle(store))) as bundle:
        traces = [json.loads(line) for line in bundle.read("traces.redacted.jsonl").splitlines()]
        assert any(t["spans"] == [{"span_id": "chain:2"}] for t in traces)
        manifest = json.loads(bundle.read("manifest.json"))
        assert manifest["projection_coverage"]["unsupported_execution_events"] > 0
