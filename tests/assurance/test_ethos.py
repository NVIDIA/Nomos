# SPDX-License-Identifier: Apache-2.0
"""Immutable purpose configuration, evidence eligibility and pinned Compass contract."""

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from assurance.ethos import EthosRegistry, InferenceConfig


@pytest.fixture
def inference():
    return InferenceConfig(
        model="openai/approved-model",
        api_base="https://approved.example/v1",
        api_key="fixture-credential",  # pragma: allowlist secret
        max_calls=2,
    )


def configure(tmp_path, *, agents=None, documents=None):
    requirements = [
        {"id": "REQUEST-01", "text": "Request staging targets only.", "evidence": ["request"]},
        {"id": "EXECUTE-01", "text": "Execute staging actions only.", "evidence": ["execution"]},
        {"id": "MESSAGE-01", "text": "Report outcomes accurately.", "evidence": ["conversation"]},
    ]
    (tmp_path / "ETHOS.md").write_text(
        "\n\n".join(f"## {r['id']}\n{r['text']}" for r in requirements)
    )
    config = {
        "schema_version": 1,
        "documents": documents
        or [
            {
                "id": "deployment",
                "version": "1",
                "path": "ETHOS.md",
                "effective_from": "2026-10-01T00:00:00Z",
                "requirements": requirements,
            }
        ],
        "agents": agents or {"agent": ["deployment@1"]},
    }
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(config))
    return path


def review_event(seq=1, *, agent="agent", timestamp="2026-10-10T00:00:00Z", context=None):
    payload = {
        "schema_version": 1,
        "agent_id": agent,
        "intent_id": "intent",
        "council_id": "test",
        "intent": {
            "agent_id": agent,
            "intent_id": "intent",
            "council_id": "test",
            "description": "Deploy staging",
            "target_environment": "staging",
            "command": "deploy staging",
        },
        "final": {"decision": "approve"},
    }
    if context is not None:
        payload["assurance_context"] = context
    return {
        "sequence": seq,
        "entry_type": "intent_review_recorded",
        "timestamp": timestamp,
        "content_hash": f"sha256:{seq:064x}",
        "payload": payload,
    }


def test_registry_snapshot_version_hash_and_export_remain_immutable(tmp_path):
    path = configure(tmp_path)
    registry = EthosRegistry.from_file(path)
    before = registry.export_documents()
    (tmp_path / "ETHOS.md").write_text("changed")
    assert registry.export_documents() == before
    assert registry.resolve("unknown", "2026-10-10T00:00:00Z") is None
    assert registry.resolve("agent", "2026-09-30T00:00:00Z") is None
    doc = registry.resolve("agent", "2026-10-10T00:00:00Z")
    assert doc.config.version == "1"
    assert doc.sha256 in doc.filename
    assert registry.manifest()["digest"] == registry.digest
    with pytest.raises(TypeError):
        registry.agents["attacker"] = ("deployment@1",)


def test_unknown_document_and_missing_requirement_text_rejected(tmp_path):
    with pytest.raises(ValueError, match="unavailable document"):
        EthosRegistry.from_file(configure(tmp_path, agents={"agent": ["absent@1"]}))
    path = configure(tmp_path)
    (tmp_path / "ETHOS.md").write_text("## REQUEST-01\nA different rule")
    with pytest.raises(ValueError, match="verbatim"):
        EthosRegistry.from_file(path)


def test_historical_versions_require_nonoverlapping_effective_intervals(tmp_path):
    path = configure(tmp_path)
    raw = json.loads(path.read_text())
    old = raw["documents"][0]
    new = {**old, "version": "2", "effective_from": "2026-10-15T00:00:00Z"}
    raw["documents"].append(new)
    raw["agents"]["agent"].append("deployment@2")
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="overlapping"):
        EthosRegistry.from_file(path)
    old["effective_until"] = new["effective_from"]
    path.write_text(json.dumps(raw))
    registry = EthosRegistry.from_file(path)
    assert registry.resolve("agent", "2026-10-14T23:59:59Z").config.version == "1"
    assert registry.resolve("agent", "2026-10-15T00:00:00Z").config.version == "2"


@pytest.mark.parametrize(
    "changes",
    [
        {"api_base": "https://user:password@host/v1"},  # pragma: allowlist secret
        {"api_base": "https://host/v1?token=secret"},
        {"api_key": ""},
        {"model": "unqualified"},
        {"max_calls": 0},
        {"request_timeout": 0},
    ],
)
def test_inference_requires_explicit_bounded_config(inference, changes):
    with pytest.raises(ValidationError):
        InferenceConfig(**{**inference.model_dump(), **changes})


def test_inference_manifests_omit_credentials(inference):
    assert "fixture-credential" not in str(inference.manifest())
    assert "fixture-credential" not in repr(inference)


def test_inference_runtime_upgrade_changes_analysis_scope(monkeypatch, inference):
    from assurance import ethos
    from assurance.scheduler import digest

    versions = {"litellm": "1.106.0", "nooa": "0.0.10"}
    monkeypatch.setattr(ethos, "version", versions.__getitem__)
    before = inference.manifest()
    versions["litellm"] = "1.105.1"
    after = inference.manifest()
    assert digest(before) != digest(after)
    assert before["runtime"]["litellm"] == "1.106.0"
    assert after["runtime"]["litellm"] == "1.105.1"
    assert "api_key" not in after


def test_inference_manifest_records_missing_optional_runtime(monkeypatch, inference):
    from assurance import ethos

    def missing(package):
        raise ethos.PackageNotFoundError(package)

    monkeypatch.setattr(ethos, "version", missing)
    assert inference.manifest()["runtime"] == {
        "litellm": "not-installed",
        "nooa": "not-installed",
    }


@pytest.mark.asyncio
async def test_per_requirement_eligibility_and_candidate_attribution(
    tmp_path, monkeypatch, inference
):
    pytest.importorskip("trace_ingest")
    from assurance.ethos import analyze_ethos

    registry = EthosRegistry.from_file(configure(tmp_path))
    seen = []

    async def detect(snapshot, text, config, budget):
        seen.append(text)
        traces = list(snapshot)
        assert traces[0].root_spans[0].input["target_environment"] == "staging"
        return SimpleNamespace(
            problems=[
                SimpleNamespace(
                    description="Observed production target conflicts with the request boundary.",
                    supporting_trace_ids=(traces[0].id,),
                    candidate_trace_ids=(),
                )
            ]
        )

    monkeypatch.setattr("assurance.ethos._detect", detect)
    findings, coverage = await analyze_ethos([review_event()], "ledger-test", registry, inference)
    assert len(seen) == len(findings) == 1
    assert "REQUEST-01" in seen[0] and "EXECUTE-01" not in seen[0]
    assert findings[0]["ethos"]["requirement_id"] == "REQUEST-01"
    assert findings[0]["candidate"] is True
    assert findings[0]["evidence"][0]["sequence"] == 1
    assert [r["status"] for r in coverage["requirements"]] == [
        "complete",
        "insufficient_evidence",
        "insufficient_evidence",
    ]
    assert "fixture-credential" not in str(findings)


@pytest.mark.asyncio
async def test_missing_mapping_skips_without_model_calls(tmp_path, monkeypatch, inference):
    pytest.importorskip("trace_ingest")
    from assurance.ethos import analyze_ethos

    async def forbidden(*args):
        raise AssertionError("unmapped agent must not call model")

    monkeypatch.setattr("assurance.ethos._detect", forbidden)
    registry = EthosRegistry.from_file(configure(tmp_path))
    findings, coverage = await analyze_ethos(
        [review_event(agent="unknown")], "ledger-test", registry, inference
    )
    assert not findings
    assert coverage["status"] == "not_configured"


@pytest.mark.asyncio
async def test_bad_model_citations_fail_instead_of_publishing(tmp_path, monkeypatch, inference):
    pytest.importorskip("trace_ingest")
    from assurance.ethos import analyze_ethos

    async def detect(*args):
        return SimpleNamespace(
            problems=[
                SimpleNamespace(
                    description="Unsupported",
                    supporting_trace_ids=("invented",),
                    candidate_trace_ids=(),
                )
            ]
        )

    monkeypatch.setattr("assurance.ethos._detect", detect)
    with pytest.raises(ValueError, match="outside supplied scope"):
        await analyze_ethos(
            [review_event()], "ledger-test", EthosRegistry.from_file(configure(tmp_path)), inference
        )


@pytest.mark.asyncio
async def test_partial_conversation_does_not_enable_reporting_requirement(
    tmp_path, monkeypatch, inference
):
    pytest.importorskip("trace_ingest")
    from assurance.ethos import analyze_ethos

    seen = []

    async def detect(snapshot, text, *_):
        seen.append(text)
        return SimpleNamespace(problems=[])

    monkeypatch.setattr("assurance.ethos._detect", detect)
    context = {
        "schema_version": 1,
        "messages": [{"role": "user", "content": "Deploy?"}],
        "conversation_complete": False,
    }
    event = review_event(context=context)
    _, coverage = await analyze_ethos(
        [event], "ledger-test", EthosRegistry.from_file(configure(tmp_path)), inference
    )
    assert len(seen) == 1
    assert coverage["requirements"][2]["status"] == "insufficient_evidence"


@pytest.mark.asyncio
async def test_successful_execution_can_be_ethos_candidate(tmp_path, monkeypatch, inference):
    pytest.importorskip("trace_ingest")
    from assurance.ethos import analyze_ethos

    async def detect(snapshot, text, *_):
        assert "EXECUTE-01" in text
        trace = next(iter(snapshot))
        assert trace.root_spans[0].output["success"] is True
        return SimpleNamespace(
            problems=[
                SimpleNamespace(
                    description="Executed a production deployment.",
                    supporting_trace_ids=(trace.id,),
                    candidate_trace_ids=(),
                )
            ]
        )

    monkeypatch.setattr("assurance.ethos._detect", detect)
    event = review_event()
    event.update(entry_type="execution_completed")
    event["payload"] = {
        "assurance_schema": 1,
        "agent_id": "agent",
        "run_id": "run",
        "logical_case_id": "case",
        "tool_name": "deploy",
        "call_id": "call",
        "input": {"environment": "production"},
        "output": {"success": True},
    }
    findings, _ = await analyze_ethos(
        [event], "ledger-test", EthosRegistry.from_file(configure(tmp_path)), inference
    )
    assert findings[0]["kind"] == "ethos_divergence"


@pytest.mark.asyncio
async def test_pinned_ethos_component_uses_immutable_scoped_text(tmp_path, monkeypatch, inference):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    pytest.importorskip("trace_ingest")
    pytest.importorskip("nooa")
    from trace_ingest.models import TraceSnapshot

    from assurance.ethos import _detect

    seen = []

    class FakeDetector:
        def __init__(self, llm):
            self.llm = llm

        async def detect_issues(self, snapshot, issue_description, **extra):
            seen.append(extra["ethos"])
            return []

    monkeypatch.setattr(
        "insight_agent.compass_signals.ethos_divergence.ethos_divergence_detector.IssueDetector",
        FakeDetector,
    )
    result = await _detect(TraceSnapshot([]), "immutable requirement", inference, {"calls": 0})
    assert result.signal_name == "ethos-divergence"
    assert seen == ["immutable requirement"]


@pytest.mark.asyncio
async def test_model_request_budget_and_no_internal_retries(monkeypatch, inference):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    pytest.importorskip("nooa")
    from nooa.unifiedllm import CompletionClient

    from assurance.ethos import _build_llm

    async def fake_call(self, *args, **kwargs):
        return "response"

    monkeypatch.setattr(CompletionClient, "acall", fake_call)
    budget = {"calls": 0}
    async with _build_llm(inference, budget) as client:
        assert client.retry_config.max_retries == client.retry_config.rate_limit_extra_retries == 0
        assert client.config["api_base"] == inference.api_base
        assert await client.acall([]) == "response"
        assert await client.acall([]) == "response"
        with pytest.raises(RuntimeError, match="budget exhausted"):
            await client.acall([])
    assert budget["calls"] == 2


@pytest.mark.asyncio
async def test_request_timeout_is_enforced(monkeypatch, inference):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    pytest.importorskip("nooa")
    from nooa.unifiedllm import CompletionClient

    from assurance.ethos import _build_llm

    async def blocked(*args, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(CompletionClient, "acall", blocked)
    config = inference.model_copy(update={"request_timeout": 0.01})
    async with _build_llm(config, {"calls": 0}) as client:
        with pytest.raises(TimeoutError):
            await client.acall([])


@pytest.mark.asyncio
async def test_version_boundary_splits_one_recorded_run(tmp_path, monkeypatch, inference):
    pytest.importorskip("trace_ingest")
    from assurance.ethos import analyze_ethos

    path = configure(tmp_path)
    raw = json.loads(path.read_text())
    first = raw["documents"][0]
    first["effective_until"] = "2026-10-15T00:00:00Z"
    raw["documents"].append(
        {
            **first,
            "version": "2",
            "effective_from": first["effective_until"],
            "effective_until": None,
        }
    )
    raw["agents"]["agent"].append("deployment@2")
    path.write_text(json.dumps(raw))
    entries = []
    for seq, timestamp in [(1, "2026-10-14T23:59:59Z"), (2, "2026-10-15T00:00:01Z")]:
        entry = review_event(seq=seq, timestamp=timestamp)
        entry["entry_type"] = "execution_completed"
        entry["payload"] = {
            "assurance_schema": 1,
            "agent_id": "agent",
            "run_id": "same-run",
            "logical_case_id": "same-case",
            "tool_name": "deploy",
            "call_id": f"call-{seq}",
            "output": {"success": True},
        }
        entries.append(entry)
    seen = []

    async def detect(snapshot, text, *_):
        traces = list(snapshot)
        seen.append((text.splitlines()[0], len(traces[0].root_spans)))
        return SimpleNamespace(problems=[])

    monkeypatch.setattr("assurance.ethos._detect", detect)
    _, coverage = await analyze_ethos(
        entries, "ledger-test", EthosRegistry.from_file(path), inference
    )
    assert seen == [("# ETHOS deployment version 1", 1), ("# ETHOS deployment version 2", 1)]
    assert len([r for r in coverage["requirements"] if r["status"] == "complete"]) == 2


@pytest.mark.asyncio
async def test_failed_inference_reports_attempted_calls_and_sanitized_error(
    tmp_path, monkeypatch, inference
):
    pytest.importorskip("trace_ingest")
    from assurance.ethos import EthosAnalysisError, analyze_ethos

    async def failure(snapshot, text, config, budget):
        budget["calls"] += 1
        raise RuntimeError("provider failure containing fixture-credential")

    monkeypatch.setattr("assurance.ethos._detect", failure)
    registry = EthosRegistry.from_file(configure(tmp_path))
    with pytest.raises(EthosAnalysisError) as caught:
        await analyze_ethos([review_event()], "ledger-test", registry, inference)
    assert caught.value.coverage["model_requests"] == 1
    assert caught.value.coverage["status"] == "failed"
    assert "fixture-credential" not in str(caught.value)


def test_enabled_ethos_import_has_no_unapproved_network_or_content_exporters():
    if importlib.util.find_spec("nooa") is None or importlib.util.find_spec("trace_ingest") is None:
        pytest.skip("optional pinned Compass inference dependencies required")
    script = """
import socket
import sys
from assurance.ethos import InferenceConfig, _build_llm

def blocked(*args, **kwargs):
    raise AssertionError('Unexpected inference-import network access')
socket.socket.connect = blocked
socket.socket.connect_ex = blocked
socket.getaddrinfo = blocked
config = InferenceConfig(model='openai/fixture', api_base='https://approved.example/v1',
                         api_key='fixture-only')  # pragma: allowlist secret
client = _build_llm(config, {'calls': 0})
from insight_agent.compass_signals.ethos_divergence.ethos_divergence_detector import (
    EthosDivergenceCompassSignal,
)
assert 'insight_agent.telemetry' not in sys.modules
import litellm
assert not litellm.success_callback
assert not litellm.failure_callback
client.close()
print('bounded-import-ok')
"""
    environment = dict(os.environ)
    environment.pop("LITELLM_LOCAL_MODEL_COST_MAP", None)
    environment.update(
        COMPASS_TELEMETRY_LANGSMITH_API_KEY="fixture-only",  # pragma: allowlist secret
        COMPASS_TELEMETRY_LANGSMITH_PROJECT="must-not-enable-exporters",
        LANGSMITH_TRACING="true",
        LANGCHAIN_TRACING_V2="true",
    )
    result = subprocess.run(
        [sys.executable, "-c", script], env=environment, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "bounded-import-ok"
    assert "model cost map fetch" not in result.stderr


@pytest.mark.asyncio
async def test_successful_requirement_cache_survives_retry_and_restart(
    tmp_path, monkeypatch, inference
):
    pytest.importorskip("trace_ingest")
    from insight_agent.compass_signals.compass_signals import CompassSignalResult

    from assurance.ethos import EthosAnalysisError, analyze_ethos
    from assurance.monitor import Store

    registry = EthosRegistry.from_file(configure(tmp_path))
    store = Store(tmp_path / "state.db", "ledger-test")
    event = review_event(
        context={
            "schema_version": 1,
            "conversation_complete": True,
            "messages": [
                {"role": "user", "content": "Deploy staging"},
                {"role": "assistant", "content": "Requested, not executed"},
            ],
        }
    )
    calls = []
    fail_message = True

    async def detect(snapshot, text, config, budget):
        budget["calls"] += 1
        requirement = "REQUEST-01" if "REQUEST-01" in text else "MESSAGE-01"
        calls.append(requirement)
        if requirement == "MESSAGE-01" and fail_message:
            raise RuntimeError("temporary model outage")
        return CompassSignalResult(signal_name="ethos-divergence", problems=())

    monkeypatch.setattr("assurance.ethos._detect", detect)
    with pytest.raises(EthosAnalysisError) as caught:
        await analyze_ethos([event], "ledger-test", registry, inference, cache=store)
    assert caught.value.coverage["model_requests"] == 2
    fail_message = False
    restarted = Store(store.path, "ledger-test")
    findings, coverage = await analyze_ethos(
        [event], "ledger-test", registry, inference, cache=restarted
    )
    assert not findings
    assert coverage["cached_requirement_checks"] == 1
    assert coverage["model_requests"] == 1
    assert calls == ["REQUEST-01", "MESSAGE-01", "MESSAGE-01"]
    _, again = await analyze_ethos([event], "ledger-test", registry, inference, cache=restarted)
    assert again["cached_requirement_checks"] == 2
    assert again["model_requests"] == 0
    changed = inference.model_copy(update={"model": "openai/different-model"})
    _, changed_coverage = await analyze_ethos(
        [event], "ledger-test", registry, changed, cache=restarted
    )
    assert changed_coverage["cached_requirement_checks"] == 0
    assert changed_coverage["model_requests"] == 2
    # Repairing the runtime must also invalidate successful cached checks for
    # the same model, document, and evidence, including across store restarts.
    versions = {"litellm": "repaired-runtime", "nooa": "0.0.10"}
    monkeypatch.setattr("assurance.ethos.version", versions.__getitem__)
    _, repaired = await analyze_ethos(
        [event], "ledger-test", registry, inference, cache=Store(store.path, "ledger-test")
    )
    assert repaired["cached_requirement_checks"] == 0
    assert repaired["model_requests"] == 2


@pytest.mark.parametrize("endpoint", ["http://otlp.example:4318/v1/traces", None])
def test_real_issue_detector_never_probes_or_exports_to_reachable_otlp(endpoint):
    if importlib.util.find_spec("nooa") is None or importlib.util.find_spec("trace_ingest") is None:
        pytest.skip("optional pinned Compass inference dependencies required")
    # Run fresh: Agent's once-per-process auto-tracing flag must initially be
    # unset. A reachable mock transport would accept health, spans and journal
    # messages. Block sockets as a second guard against any unrelated egress.
    script = """
import asyncio
import socket
import urllib.request
from assurance.ethos import InferenceConfig, _build_llm

def blocked(*args, **kwargs):
    raise AssertionError('Unexpected socket access')
socket.socket.connect = blocked
socket.socket.connect_ex = blocked
socket.getaddrinfo = blocked
requests = []
class ReachableResponse:
    status = 200
    headers = {}
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self):
        return b'{}'
def reachable(request, *args, **kwargs):
    requests.append(request.full_url)
    return ReachableResponse()
urllib.request.urlopen = reachable
config = InferenceConfig(model='openai/fixture', api_base='https://approved.example/v1',
                         api_key='fixture-only')  # pragma: allowlist secret
client = _build_llm(config, {'calls': 0})
from insight_agent.compass_signals.issue_detector import IssueDetector
from nooa.tracing import flush_traces
from opentelemetry import trace
import litellm

# This is the real NOOA Agent constructor, including auto-tracing setup.
detector = IssueDetector(llm=client)
assert detector.llm is client
with trace.get_tracer('assurance-regression').start_as_current_span('sensitive-ETHOS') as span:
    assert not span.is_recording()
async def inference():
    response = await client.acall([{'role': 'user', 'content': 'sensitive ledger context'}],
                                 mock_response='fixture response')
    assert response.content == 'fixture response'
    await client.aclose()
asyncio.run(inference())
flush_traces()
assert requests == [], requests
assert not litellm.callbacks, litellm.callbacks
assert not litellm.success_callback
assert not litellm.failure_callback
print('real-detector-no-export-ok')
"""
    environment = dict(os.environ)
    for setting in ("LITELLM_LOCAL_MODEL_COST_MAP", "OTEL_SDK_DISABLED", "OTLP_ENDPOINT"):
        environment.pop(setting, None)
    environment.update(
        COMPASS_TELEMETRY_LANGSMITH_API_KEY="fixture-only",  # pragma: allowlist secret
        COMPASS_TELEMETRY_LANGSMITH_PROJECT="must-not-enable-exporters",
        LANGSMITH_TRACING="true",
        LANGCHAIN_TRACING_V2="true",
    )
    if endpoint is not None:
        environment["OTLP_ENDPOINT"] = endpoint
    result = subprocess.run(
        [sys.executable, "-c", script], env=environment, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "real-detector-no-export-ok"
