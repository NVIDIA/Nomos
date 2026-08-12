# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from nomos.llm.council_agent import _parse_chairman_decision, _parse_evaluation


def test_parse_evaluation_accepts_trailing_text_after_json():
    response = """
    {
      "decision": "approve",
      "reasoning": "The command is read-only and tightly scoped. The target matches the request.",
      "risk_concerns": [],
      "conditions": [],
      "guidance": ""
    }
    Extra commentary the model should not have returned.
    """

    parsed = _parse_evaluation(response)

    assert parsed.decision == "approve"
    assert "read-only" in parsed.reasoning


def test_parse_chairman_decision_accepts_fenced_json_with_trailing_text():
    response = """```json
    {
      "threshold": "quorum",
      "reasoning": "This is a bounded planning step with no live infrastructure changes.",
      "risk_level": "low"
    }
    ```
    Additional notes.
    """

    parsed = _parse_chairman_decision(response)

    assert parsed.threshold == "quorum"
    assert parsed.risk_level == "low"
