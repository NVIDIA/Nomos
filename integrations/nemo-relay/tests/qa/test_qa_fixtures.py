# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Self-check: the QA fixtures produce writs and results the real pinned Nomos models accept."""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest
import qa_support as qa

# The Nomos source: the enclosing Nomos (integrations/nemo-relay), or the pinned submodule (standalone repository).
VENDOR = next(
    (
        source
        for parent in Path(__file__).resolve().parents
        for source in (parent, parent / "vendor" / "nomos")
        if (source / "trusted_executor_contract" / "models.py").exists()
    ),
    Path(__file__).resolve().parents[2] / "vendor" / "nomos",
)


@pytest.fixture
def nomos_models():
    if not (VENDOR / "trusted_executor_contract" / "models.py").exists():
        pytest.skip(f"pinned Nomos submodule not present at {VENDOR}")
    sys.path.insert(0, str(VENDOR))
    try:
        from trusted_executor_contract import models
    finally:
        sys.path.remove(str(VENDOR))
    return models


def test_fixture_writ_verifies_under_nomos_writ_of_execution(nomos_models):
    writ = qa.mint_writ("echo hi")
    model = nomos_models.WritOfExecution.model_validate_json(base64.b64decode(qa.encode_writ(writ)))
    assert model.canonical_bytes() == qa.writ_canonical_bytes(writ), f"canonical bytes differ for {writ!r}"
    assert model.verify(qa.public_bytes(qa.COUNCIL_SEED)), f"Nomos rejects fixture writ {writ!r}"


def test_fixture_result_verifies_under_nomos_execution_evidence(nomos_models):
    writ = qa.mint_writ("echo hi")
    result = qa.signed_result(writ, stdout="out\n", stderr="err")
    model = nomos_models.ExecutionResult.model_validate(result)
    assert model.evidence is not None and model.evidence.verify(), f"Nomos rejects fixture result {result!r}"
    assert model.evidence.payload.canonical_bytes() == qa.canonical(result["evidence"]["payload"])
