# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Pin evidence preservation, instruction isolation and measurement accounting."""

import copy
import json
import unittest
from pathlib import Path

from run_pilot import build_cells, make_state, normalized_context, parse_scores, prose_context

ROOT = Path(__file__).resolve().parents[1]


class StudyContract(unittest.TestCase):
    """Compare actual fixture transformations rather than expected model judgments."""

    def setUp(self):
        self.cases = json.loads((ROOT / "cases.json").read_text())["cases"]
        self.catalogs = json.loads((ROOT / "questions.json").read_text())["catalogs"]

    def test_format_changes_only_supplementary_context_and_never_mutates_source(self):
        """HTTP observation and original fixture dictionaries survive all transforms."""
        case = copy.deepcopy(self.cases[1])
        before = copy.deepcopy(case)
        states = [
            make_state(case, name) for name in ("http_only", "full_json", "compact_json", "prose")
        ]
        bases = [
            {k: v for k, v in state.items() if k != "conversation_context"} for state in states
        ]
        self.assertTrue(all(base == bases[0] for base in bases))
        self.assertEqual(case, before)
        self.assertNotIn("conversation_context", states[0])
        self.assertNotIn("expected", json.dumps(states))
        self.assertNotIn("label_basis", json.dumps(states))

    def test_prose_and_compact_have_the_same_normalized_evidence(self):
        """Every normalized evidence leaf remains represented in the prose rendering."""
        context = normalized_context(self.cases[5]["context"])
        prose = prose_context(context)

        def strings(value):
            if isinstance(value, dict):
                return [leaf for child in value.values() for leaf in strings(child)]
            if isinstance(value, list):
                return [leaf for child in value for leaf in strings(child)]
            return [value]

        for value in strings(context):
            self.assertIn(json.dumps(value, ensure_ascii=False), prose)
        self.assertIn("nms_key_", prose)
        self.assertEqual(len(context["model_requests"][0]["tool_calls"]), 2)

    def test_missing_context_does_not_invent_history(self):
        """A genuine HTTP-only captured state stays HTTP-only in every format."""
        case = self.cases[0]
        captured = make_state(case, "http_only")
        for name in ("full_json", "compact_json", "prose"):
            self.assertEqual(make_state(case, name, captured), captured)

    def test_grouping_preserves_current_and_earlier_parent_relationships(self):
        """Dropped UUIDs are replaced by explicit grouping, not flattened event history."""
        source = copy.deepcopy(self.cases[0]["context"])
        older = copy.deepcopy(source["recent_tool_records"][0])
        older["snapshot_id"] = "snapshot-earlier"
        source["recent_tool_records"].append(older)
        groups = normalized_context(source)["model_requests"]
        self.assertEqual(groups[0]["relationship"], "current retained model request")
        self.assertEqual(
            groups[1]["relationship"],
            "earlier model request; its retained user text is not supplied",
        )

    def test_incomplete_capture_becomes_explicit_evidence_limitations(self):
        """Unknown capture completeness remains present even when all returned records fit."""
        result = normalized_context(self.cases[-1]["context"])
        limits = " ".join(result["limitations"]) + json.dumps(result["model_requests"])
        self.assertIn("shortened", limits)
        self.assertIn("argument", limits)
        self.assertIn("not independently verified", limits)
        self.assertIn("not proof", limits)

    def test_compact_retains_call_and_council_ids_on_each_lifecycle_event(self):
        """Group a call once while preserving each event's reported Council fields."""
        source = copy.deepcopy(self.cases[0]["context"])
        record = source["recent_tool_records"][0]
        record["tool_call_id"] = "provider-call-example"
        record["state"] = "denied"
        record["council_review"] = {
            "status": "approved",
            "decision_id": "intent-example",
            "council_id": "council-example",
            "guidance": ["Keep the publication scoped to the approved audience."],
            "guidance_truncated": True,
        }
        previous = copy.deepcopy(record)
        previous["record_id"] = "event-previous"
        previous["state"] = "execution_pending"
        source["recent_tool_records"] = [record, previous]
        original = copy.deepcopy(source)
        result = normalized_context(source)
        calls = result["model_requests"][0]["tool_calls"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["tool_call_id"], "provider-call-example")
        events = calls[0]["events_newest_first"]
        self.assertEqual(
            [e["reported_execution_state"] for e in events], ["denied", "execution_pending"]
        )
        for event in events:
            self.assertEqual(event["nomos_council"], record["council_review"])
            self.assertNotIn("reported_council_decision", event)
        prose = prose_context(result)
        for value in ("provider-call-example", "intent-example", "council-example"):
            self.assertIn(json.dumps(value), prose)
        self.assertIn("guidance_truncated: true", prose)
        self.assertEqual(source, original)
        events[0]["nomos_council"]["guidance"].append("changed")
        self.assertEqual(source, original)

    def test_missing_correlation_ids_and_reviews_are_not_invented(self):
        """Absent call IDs stay null; unknown Council fields do not imply approval."""
        for review in (None, {"status": "error", "decision_id": None}):
            with self.subTest(review=review):
                source = copy.deepcopy(self.cases[0]["context"])
                record = source["recent_tool_records"][0]
                record.pop("tool_call_id", None)
                record["council_review"] = review
                source["recent_tool_records"] = [record, copy.deepcopy(record)]
                calls = normalized_context(source)["model_requests"][0]["tool_calls"]
                self.assertEqual(len(calls), 2)
                for call in calls:
                    self.assertIsNone(call["tool_call_id"])
                    self.assertEqual(call["events_newest_first"][0]["nomos_council"], review)

    def test_identical_baseline_requests_share_one_measurement_without_salts(self):
        """Paired cases with the same outgoing data must not pretend to be independent calls."""
        cells, requests = build_cells(self.cases, self.catalogs, ["http_only"], "jev-1.14.0", 7)
        self.assertLess(len(requests), len(cells))
        for cell in cells:
            request = requests[cell["request_sha256"]]
            self.assertNotIn("expected", request)
            self.assertNotIn("nonce", json.dumps(request))
        first = {(c["case_id"], c["policy"]): c["request_sha256"] for c in cells}
        self.assertEqual(first[("ctx-01", "baseline")], first[("ctx-02", "baseline")])

    def test_score_parser_requires_identity_and_finite_bounded_noul_answers(self):
        """Model alias drift, absent questions and non-probability values remain failures."""
        response = {"model": "jev-1.14.0", "answers": {"risk": {"type": "noul", "noul": 0.7}}}
        self.assertEqual(parse_scores(response, "jev-1.14.0", ["risk"]), {"risk": 0.7})
        for bad in (True, -0.1, 1.1, float("nan")):
            response["answers"]["risk"]["noul"] = bad
            with self.assertRaises(ValueError):
                parse_scores(response, "jev-1.14.0", ["risk"])
        with self.assertRaises(ValueError):
            parse_scores({"model": "another", "answers": {}}, "jev-1.14.0", ["risk"])


if __name__ == "__main__":
    unittest.main()
