# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"Check labels, paired state invariance, catalog scope and immutable preparation."

import copy
import json
import shlex
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
        """Each category has eight distinct content families, safe/unsafe siblings."""
        cases = build_cases.build()["cases"]
        self.assertEqual(len(cases), 96)
        self.assertEqual(len({study.helpers.digest(c["http"]) for c in cases}), 96)
        counts = Counter((c["category"], c["expected"]) for c in cases)
        self.assertEqual(set(counts.values()), {8})
        self.assertEqual(len(counts), 12)
        self.assertEqual(set(Counter(c["pair_id"] for c in cases).values()), {2})
        for c in cases:
            self.assertEqual(any(c["expected_rules"].values()), c["expected"] == "deny")

    def test_existing_48_cases_remain_unchanged_except_cohort_metadata(self):
        "Expansion preserves definitions rather than repairing previously missed cases."
        original = json.loads((study.ROOT / "data/input-snapshot/cases.json").read_text())["cases"]
        expanded = build_cases.build()["cases"]
        self.assertEqual(
            [{k: v for k, v in c.items() if k != "cohort"} for c in expanded[:48]], original
        )
        self.assertEqual(Counter(c["cohort"] for c in expanded), {"existing48": 48, "new48": 48})

    def test_new_terminal_history_uses_valid_shell_quoting(self):
        """New JSON-writing commands retain apostrophes inside one shell argument."""
        for case in build_cases.build()["cases"][48:]:
            for record in case["context"]["recent_tool_records"]:
                command = record["arguments"]["command"]
                words = shlex.split(command)
                if command.startswith("printf '%s' ") and words[2].startswith("{"):
                    self.assertIsInstance(json.loads(words[2]), dict)
                    self.assertEqual(words[3:], [">", "/tmp/local-draft.json"])

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
        "Three arms preserve paired states and balance six orders within four strata."
        cases = build_cases.build()["cases"]
        states = {c["id"]: study.helpers.make_state(c, "full_json") for c in cases}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            captured = root / "states.json"
            captured.write_text(json.dumps({"states": states}))
            output = root / "run"
            study.prepare(output, captured)
            cells = json.loads((output / "matrix.json").read_text())
            self.assertEqual(len(cells), 288)
            self.assertEqual(len({c["request_sha256"] for c in cells}), 288)
            orders = Counter()
            strata = Counter()
            for i in range(0, 288, 3):
                group = cells[i : i + 3]
                order = tuple(c["policy"] for c in group)
                orders[order] += 1
                strata[(group[0]["cohort"], group[0]["expected"], order)] += 1
                requests = {
                    c["policy"]: json.loads(
                        (output / "requests" / (c["request_sha256"] + ".json")).read_text()
                    )
                    for c in group
                }
                control, candidate, http = [requests[p] for p in study.CONDITIONS]
                self.assertEqual(control["state"], candidate["state"])
                stripped = copy.deepcopy(control["state"])
                stripped.pop("conversation_context")
                self.assertEqual(stripped, http["state"])
                self.assertEqual(control["questions"], http["questions"])
                for request in requests.values():
                    self.assertFalse(
                        set(request["state"])
                        & {"expected", "expected_rules", "label_basis", "category", "cohort"}
                    )
                    self.assertNotIn("tool_definitions_kept", json.dumps(request))
                    self.assertNotIn("tool_definitions_sha256", json.dumps(request))
            self.assertEqual(len(orders), 6)
            self.assertEqual(set(orders.values()), {16})
            self.assertEqual(len(strata), 24)
            self.assertEqual(set(strata.values()), {4})
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
