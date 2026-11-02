# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Protect report error accounting and inert readable evidence rendering."""

import unittest

import build_report


class ReportContract(unittest.TestCase):
    """Keep missing/failed assessments separate from policy decisions and timing."""

    def test_failure_is_not_a_denial_or_successful_latency(self):
        """An unavailable response contributes a failure, not an invented verdict."""
        ok = {
            "expected": "allow",
            "decision": "allow",
            "matches_expected": True,
            "measurement": {
                "status": "ok",
                "latency_ms": 100,
                "usage": {"input_tokens": 12, "output_tokens": 3},
            },
        }
        result = build_report.summarize([ok, ok, {"measurement": None}])
        self.assertEqual(result["correct"], 2)
        self.assertEqual(result["failures"], 1)
        self.assertEqual(result["false_blocks"], 0)
        self.assertEqual(result["escapes"], 0)
        self.assertEqual(result["input_tokens"], 24)
        self.assertEqual(result["mean_ms"], 100)

    def test_model_evidence_cannot_execute_html(self):
        """Nested strings are text and retain real newlines rather than escapes."""
        output = build_report.readable({"command": "<script>alert(1)</script>\nnext"})
        self.assertNotIn("<script>", output)
        self.assertIn("&lt;script&gt;", output)
        self.assertIn("\nnext", output)


if __name__ == "__main__":
    unittest.main()
