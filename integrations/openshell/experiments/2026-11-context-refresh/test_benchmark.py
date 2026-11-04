# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Verify matched input contrasts and immutable once-only execution offline."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import benchmark


class RefreshContract(unittest.TestCase):
    """Check actual prepared requests before spending live inference calls."""

    def test_input_contrasts_and_unique_call_accounting(self):
        """Only the intended context or questions differ between matched arms."""
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "prepared"
            benchmark.prepare(output)
            cells = json.loads((output / "matrix.json").read_text())
            self.assertEqual(len(cells), 528)
            calls = json.loads((output / "call_order.json").read_text())
            self.assertEqual(len(calls), len({c["request_sha256"] for c in cells}))
            self.assertEqual(len(calls), len({c["request_sha256"] for c in calls}))
            for start in range(0, len(cells), 4):
                group = {
                    c["arm"]: json.loads(
                        (output / "requests" / (c["request_sha256"] + ".json")).read_text()
                    )
                    for c in cells[start : start + 4]
                }
                original, guided, ablated, plain = [group[arm] for arm in benchmark.ARMS]
                self.assertEqual(original["state"], guided["state"])
                base = copy.deepcopy(original["state"])
                base.pop("conversation_context")
                self.assertEqual(base, plain["state"])
                reduced = copy.deepcopy(original["state"])
                for request in reduced["conversation_context"]["model_requests"]:
                    for call in request["tool_calls"]:
                        for event in call["events_newest_first"]:
                            event.pop("nomos_council", None)
                self.assertEqual(reduced, ablated["state"])
                self.assertEqual(original["questions"], plain["questions"])
                self.assertEqual(original["questions"], ablated["questions"])
                for rule, question in original["questions"].items():
                    self.assertTrue(
                        guided["questions"][rule]["instructions"].endswith(question["instructions"])
                    )
                # The scope mentions the field; actual timestamp fields are absent.
                self.assertNotIn("snapshot_recorded_at", original["state"]["conversation_context"])
            with self.assertRaises(ValueError):
                benchmark.prepare(output)

    def test_failed_attempt_is_retained_and_cannot_silently_retry(self):
        """An unsuccessful call stops the run and preserves its actual status."""
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "prepared"
            benchmark.prepare(output)

            def fail(request, key, timeout):
                return {
                    "request_sha256": benchmark.helpers.digest(request),
                    "status": "http_error",
                }, "Unavailable"

            with (
                mock.patch.dict(benchmark.os.environ, {"JEV_API_KEY": "nonworking-test-value"}),
                mock.patch.object(benchmark.helpers, "assess", side_effect=fail) as assess,
            ):
                with self.assertRaisesRegex(RuntimeError, "benchmark incomplete"):
                    benchmark.execute(output)
                assess.assert_called_once()
                with self.assertRaises(ValueError):
                    benchmark.execute(output)
            self.assertEqual(len((output / "attempts.jsonl").read_text().splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
