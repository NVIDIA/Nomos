# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Protect historical artifacts and keep report claims tied to the selected run."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

import build_report
import plot_figures


class ReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = build_report.ROOT / "data/measured-v1"
        cls.result = json.loads((cls.data / "results.json").read_text())
        cls.fixtures = json.loads((cls.data / "input-snapshot/fixtures.json").read_text())["cases"]

    def test_example_uses_selected_values_scores_and_pending_arguments(self):
        """Changing selected evidence must change the example, not reuse v1 literals."""
        cells = copy.deepcopy(self.result["cells"])
        fixtures = copy.deepcopy(self.fixtures)
        sample = next(
            c
            for c in cells
            if c["case_id"] == "role-01-credential-role" and c["arm"] == "original_context"
        )
        sha = sample["request_sha256"]
        request = json.loads((self.data / "requests" / (sha + ".json")).read_text())
        sample.update(
            scores={"credential_disclosure": 0.23, "confidential_disclosure": 0.12},
            decision="allow",
            matches_expected=False,
        )
        fixtures[0]["http"]["body"]["value"] = "selected-fixture-value"
        request["state"]["conversation_context"]["model_requests"][0]["tool_calls"][0][
            "events_newest_first"
        ][0]["arguments"] = {"command": "python /workspace/publish.py"}
        example = build_report.worked_example(fixtures, cells, {sha: request})
        self.assertIn("selected-fixture-value", example)
        self.assertIn("0.23 allow ✗", example)
        self.assertIn("python /workspace/publish.py", example)

    def test_v2_cannot_overwrite_original_report_or_figures(self):
        result = copy.deepcopy(self.result)
        result["manifest"]["dataset"] = "normal-path-pending-sender-v2"
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data / "results.json").write_text(json.dumps(result))
            with self.assertRaisesRegex(ValueError, "preserve v1"):
                build_report.render(data, build_report.ROOT / "report.html", data / "figures")
            with self.assertRaisesRegex(ValueError, "preserve v1"):
                plot_figures.plot(data, build_report.ROOT / "figures")

    def test_original_measurements_cannot_be_named_v2(self):
        with self.assertRaisesRegex(ValueError, "actual v2 measurements"):
            build_report.render(
                self.data, build_report.ROOT / "report-v2.html", build_report.ROOT / "figures"
            )

    def test_incomplete_run_is_not_a_completed_report(self):
        result = copy.deepcopy(self.result)
        result["measurements"].pop()
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data / "results.json").write_text(json.dumps(result))
            with self.assertRaisesRegex(ValueError, "all planned"):
                build_report.render(data, data / "report.html", data / "figures")

    def test_missing_frozen_fixtures_cannot_fall_back_to_current_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            for name in ("results.json", "requests", "responses"):
                (data / name).symlink_to(self.data / name)
            with self.assertRaisesRegex(ValueError, "frozen fixture snapshot"):
                build_report.render(data, data / "report.html", build_report.ROOT / "figures")


if __name__ == "__main__":
    unittest.main()
