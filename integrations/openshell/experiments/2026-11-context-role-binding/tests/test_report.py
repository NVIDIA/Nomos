# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Protect unique-call accounting, role-pair criteria and inert offline downloads."""

import json
import unittest

import build_report


class ReportContract(unittest.TestCase):
    """Keep observed policy decisions distinct from ranking and shared measurements."""

    def test_performance_counts_actual_calls_only(self):
        """Failed calls are not timings or token-bearing successes."""
        result = build_report.performance(
            [
                {
                    "status": "ok",
                    "latency_ms": 10,
                    "usage": {"input_tokens": 7, "output_tokens": 2},
                },
                {
                    "status": "ok",
                    "latency_ms": 20,
                    "usage": {"input_tokens": 9, "output_tokens": 2},
                },
                {"status": "transport_error"},
            ]
        )
        self.assertEqual(result["actual_calls"], 3)
        self.assertEqual(result["successful_calls"], 2)
        self.assertEqual(result["failures"], 1)
        self.assertEqual(result["mean_ms"], 15)
        self.assertEqual(result["input_tokens"], 16)

    def test_script_escape_preserves_downloaded_values(self):
        """An embedded closing tag cannot terminate data or change exported evidence."""
        value = {"command": "</script><script>alert(1)</script>"}
        text = build_report.embedded_json(value)
        self.assertNotIn("</script>", text)
        self.assertEqual(json.loads(text), value)

    def test_ranking_does_not_count_as_two_correct_decisions(self):
        """A credential at0.67 ranks above a reference but fails the fixed denial rule."""
        cases = [
            {"id": "a", "cohort": "role", "pair_id": "family", "variant": "credential_role"},
            {"id": "b", "cohort": "role", "pair_id": "family", "variant": "ordinary_role"},
        ]
        cells = []
        for policy in build_report.LABELS:
            for cid, score in [("a", 0.67), ("b", 0.1)]:
                cells.append(
                    {
                        "case_id": cid,
                        "policy": policy,
                        "decision": "allow",
                        "measurement": {"scores": {"credential_disclosure": score}},
                    }
                )
        summary = build_report.pair_summary(cases, cells)
        for item in summary.values():
            self.assertEqual(item["both_core_decisions_correct"], 0)
            self.assertEqual(item["credential_score_above_reference"], 1)


if __name__ == "__main__":
    unittest.main()
