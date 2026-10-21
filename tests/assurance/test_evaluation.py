# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Exercise the real deterministic adapter; never invoke external inference."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("trace_ingest", reason="optional assurance-compass extra required")

from scripts.assurance_evaluate import CHAIN, evaluate, metrics, tool_cases, valid_evidence

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_default_evaluation_has_explicit_labels_and_actual_evidence():
    report = await evaluate()
    result = report["tool_issues"]
    assert {case["case_id"] for case in result["cases"]} == {
        "unchanged-denial",
        "corrected-denial",
        "repeated-failed-call",
        "bounded-recovery",
        "receipt-duplicate",
    }
    assert result["metrics"]["true_positives"] == 2
    assert result["metrics"]["true_negatives"] == 3
    assert result["metrics"]["false_positives"] == result["metrics"]["false_negatives"] == 0
    assert result["metrics"]["precision"] == result["metrics"]["recall"] == 1
    assert (
        result["metrics"]["valid_evidence_references"] == result["metrics"]["evidence_references"]
    )
    assert result["metrics"]["evidence_references"] > 0
    assert report["ethos"] == {"status": "not_requested", "inference_requests": 0}
    assert result["metrics"]["inference_requests"] == 0
    duplicate = next(c for c in result["cases"] if c["case_id"] == "receipt-duplicate")
    assert duplicate["finding_count"] == 1
    assert duplicate["prediction"] is False
    assert report["synthetic"] is True


def test_abstentions_and_model_failures_are_not_true_negatives():
    rows = []
    for label, prediction, status in [
        (True, True, "complete"),
        (False, False, "complete"),
        (True, None, "inference_failed"),
        (None, None, "insufficient_evidence"),
        (None, False, "complete"),
    ]:
        rows.append(
            {
                "human_label": label,
                "prediction": prediction,
                "status": status,
                "inference_requests": 2 if status == "inference_failed" else 0,
                "evidence_references": 0,
                "valid_evidence_references": 0,
            }
        )
    result = metrics(rows)
    assert result["evaluated_cases"] == 2
    assert result["true_negatives"] == 1
    assert result["abstentions"] == 2
    assert result["expected_abstentions"] == 2
    assert result["abstention_contract_failures"] == 1
    assert result["inference_requests"] == 2
    assert result["inference_failures"] == 1


def test_evidence_validation_checks_chain_sequence_and_hash():
    entry = tool_cases()[0].events[0]
    valid = {
        "chain_id": CHAIN,
        "sequence": entry["sequence"],
        "content_hash": entry["content_hash"],
    }
    findings = [
        {
            "evidence": [
                valid,
                {**valid, "chain_id": "elsewhere"},
                {**valid, "content_hash": "fabricated"},
                {**valid, "sequence": 999},
            ]
        }
    ]
    assert valid_evidence(findings, [entry]) == (1, 4)


def test_cli_default_is_json_and_makes_no_inference_calls():
    env = {
        **os.environ,
        "ASSURANCE_API_KEY": "sensitive-evaluation-fixture",  # pragma: allowlist secret
    }
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/assurance_evaluate.py")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["ethos"]["inference_requests"] == 0
    assert "sensitive-evaluation-fixture" not in result.stdout + result.stderr


def test_cli_rejects_ethos_without_configuration_without_echoing_secrets():
    env = {k: v for k, v in os.environ.items() if not k.startswith("ASSURANCE_")}
    env["ASSURANCE_API_KEY"] = "sensitive-evaluation-fixture"  # pragma: allowlist secret
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/assurance_evaluate.py"), "--ethos"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "failed"
    assert "sensitive-evaluation-fixture" not in result.stdout + result.stderr


@pytest.mark.asyncio
async def test_optional_evaluation_preserves_failure_usage_and_abstains(monkeypatch):
    """Only failure-reporting glue is mocked; this is not an inference quality test."""
    from assurance.ethos import EthosAnalysisError

    monkeypatch.setenv("ASSURANCE_MODEL", "openai/fixture-model")
    monkeypatch.setenv("ASSURANCE_API_BASE", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("ASSURANCE_API_KEY", "private-fixture-credential")

    async def fail_with_usage(*args):
        raise EthosAnalysisError({"model_requests": 2})

    monkeypatch.setattr("assurance.ethos.analyze_ethos", fail_with_usage)
    report = await evaluate(with_ethos=True)
    result = report["ethos"]["metrics"]
    assert result["evaluated_cases"] == result["true_negatives"] == 0
    assert result["abstentions"] == result["inference_failures"] == 6
    assert result["inference_requests"] == 12
    assert result["precision"] is result["recall"] is None
    assert "private-fixture-credential" not in json.dumps(report)
