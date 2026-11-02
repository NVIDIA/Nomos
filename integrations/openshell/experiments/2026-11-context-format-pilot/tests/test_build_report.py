# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Keep logical outcomes distinct from actual latency/token measurements."""

import unittest

from build_report import render, summarize


class ReportAccounting(unittest.TestCase):
    """Synthetic counters exercise deduplication rather than chart implementation."""

    def test_shared_measurement_counts_once_for_performance_but_twice_for_labels(self):
        """Two case labels may reuse one assessment without duplicating its resources."""
        measurement = {
            "measurement_id": "one",
            "status": "ok",
            "latency_ms": 100,
            "usage": {"input_tokens": 50, "output_tokens": 4},
        }
        results = {
            "cells": [
                {
                    "policy": "baseline",
                    "format": "http_only",
                    "measurement": measurement,
                    "expected": "allow",
                    "decision": "allow",
                    "matches_expected": True,
                },
                {
                    "policy": "baseline",
                    "format": "http_only",
                    "measurement": measurement,
                    "expected": "deny",
                    "decision": "allow",
                    "matches_expected": False,
                },
            ]
        }
        summary = summarize(results)[0]
        self.assertEqual(summary["cases"], 2)
        self.assertEqual(summary["agreement"], 1)
        self.assertEqual(summary["false_allows"], 1)
        self.assertEqual(summary["actual_calls"], 1)
        self.assertEqual(summary["mean_latency_ms"], 100)
        self.assertEqual(summary["mean_input_tokens"], 50)

    def test_errors_remain_failures_without_latency_or_token_success_statistics(self):
        """An HTTP failure is not a policy denial or a successful timed assessment."""
        result = {
            "cells": [
                {
                    "policy": "baseline",
                    "format": "prose",
                    "measurement": {
                        "measurement_id": "error",
                        "status": "http_error",
                        "latency_ms": 20,
                    },
                    "expected": "deny",
                }
            ]
        }
        summary = summarize(result)[0]
        self.assertEqual(summary["assessment_failures"], 1)
        self.assertEqual(summary["false_blocks"], 0)
        self.assertEqual(summary["false_allows"], 0)
        self.assertIsNone(summary["mean_latency_ms"])
        self.assertIsNone(summary["mean_input_tokens"])

    def test_embedded_fixture_text_cannot_terminate_the_data_script(self):
        """Readable payloads stay inert even if a future fixture includes HTML syntax."""
        rendered = render({"fixture": "</script><script>alert(1)</script>"})
        self.assertNotIn('"fixture": "</script>', rendered)
        self.assertIn("<\\/script>", rendered)


if __name__ == "__main__":
    unittest.main()
