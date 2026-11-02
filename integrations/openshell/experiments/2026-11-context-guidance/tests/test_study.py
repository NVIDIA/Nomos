# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Check labels, paired state invariance, catalog scope and immutable preparation."""

import copy
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

import build_cases
import study


class StudyContract(unittest.TestCase):
    """Validate the study before spending calls or interpreting model decisions."""

    def test_six_balanced_categories_and_distinct_states(self):
        """Each category has four distinct content families, safe/unsafe siblings."""
        cases = build_cases.build()["cases"]
        self.assertEqual(len(cases), 48)
        self.assertEqual(len({study.helpers.digest(c["http"]) for c in cases}), 48)
        counts = Counter((c["category"], c["expected"]) for c in cases)
        self.assertEqual(set(counts.values()), {4})
        self.assertEqual(len(counts), 12)
        self.assertEqual(set(Counter(c["pair_id"] for c in cases).values()), {2})
        for c in cases:
            self.assertEqual(any(c["expected_rules"].values()), c["expected"] == "deny")

    def test_candidate_changes_only_prefix_not_risk_or_threshold(self):
        """Both conditions share the same original disclosure criteria and settings."""
        catalogs = json.loads((study.ROOT / "questions.json").read_text())["catalogs"]
        for control, candidate in zip(
            catalogs["baseline"]["rules"], catalogs["context_aware"]["rules"], strict=True
        ):
            self.assertEqual(candidate["question"], build_cases.PREFIX + " " + control["question"])
            self.assertEqual(
                {k: v for k, v in control.items() if k != "question"},
                {k: v for k, v in candidate.items() if k != "question"},
            )

    def test_preparation_is_balanced_unique_and_does_not_leak_labels(self):
        """Ninety-six requests pair identical states with exactly24 opposite orders."""
        cases = build_cases.build()["cases"]
        states = {c["id"]: study.helpers.make_state(c, "full_json") for c in cases}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            captured = root / "states.json"
            captured.write_text(json.dumps({"states": states}))
            output = root / "run"
            study.prepare(output, captured)
            cells = json.loads((output / "matrix.json").read_text())
            self.assertEqual(len(cells), 96)
            self.assertEqual(len({c["request_sha256"] for c in cells}), 96)
            self.assertEqual(sum(c["policy"] == "baseline" for c in cells[::2]), 24)
            for i in range(0, 96, 2):
                a, b = [
                    json.loads((output / "requests" / (c["request_sha256"] + ".json")).read_text())
                    for c in cells[i : i + 2]
                ]
                self.assertEqual(a["state"], b["state"])
                for request in (a, b):
                    self.assertFalse(
                        set(request["state"])
                        & {"expected", "expected_rules", "label_basis", "category"}
                    )
                    self.assertNotIn("tool_definitions_kept", json.dumps(request))
                    self.assertNotIn("tool_definitions_sha256", json.dumps(request))
            with self.assertRaises(ValueError):
                study.prepare(output, captured)

    def test_http_only_does_not_gain_candidate_prefix_at_reader_boundary(self):
        """The existing formatter does not invent context from an absent lookup."""
        c = copy.deepcopy(build_cases.build()["cases"][0])
        c["context"] = None
        self.assertNotIn("conversation_context", study.helpers.make_state(c, "compact_json"))

    def test_labels_are_derived_from_current_body_or_query(self):
        """No case relies solely on unverified publicity claims to determine a label."""
        for c in build_cases.build()["cases"]:
            self.assertFalse(c["context_dependent"])
            self.assertEqual(
                c["context"]["retained_context"]["latest_user_request"], c["agent_prompt"][:800]
            )
            self.assertEqual(
                c["context"]["retained_context"]["user_request_truncated"],
                len(c["agent_prompt"]) > 800,
            )

    def test_interrupted_attempt_is_recorded_and_cannot_be_silently_retried(self):
        """Persist the started hash before network I/O and refuse a second execution."""
        cases = build_cases.build()["cases"]
        states = {c["id"]: study.helpers.make_state(c, "full_json") for c in cases}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            captured = root / "states.json"
            captured.write_text(json.dumps({"states": states}))
            output = root / "run"
            study.prepare(output, captured)
            with mock.patch.dict(study.os.environ, {"JEV_API_KEY": "nonworking-test-value"}):
                with mock.patch.object(study.helpers, "assess", side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        study.execute(output)
                attempts = (output / "attempts.jsonl").read_text().splitlines()
                self.assertEqual(len(attempts), 1)
                self.assertEqual(json.loads(attempts[0])["event"], "started")
                with self.assertRaises(ValueError):
                    study.execute(output)


if __name__ == "__main__":
    unittest.main()
