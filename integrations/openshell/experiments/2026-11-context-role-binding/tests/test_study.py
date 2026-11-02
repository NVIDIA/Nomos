# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Verify information limits, role-only differences, deduplication and attempt safety."""

import copy
import io
import json
import shlex
import tempfile
import unittest
from collections import Counter, defaultdict
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import build_cases
import study


def cases():
    """Load the reviewed fixture artifact, not newly invented labels in a test."""
    return json.loads((study.ROOT / "cases.json").read_text())["cases"]


def prepare_fixture(root: Path) -> Path:
    """Exercise preparation offline; native component capture is checked separately."""
    captured = root / "captured.json"
    captured.write_text(
        json.dumps({"states": {c["id"]: study.helpers.make_state(c, "full_json") for c in cases()}})
    )
    output = root / "run"
    with redirect_stdout(io.StringIO()):
        study.prepare(output, captured)
    return output


class StudyContract(unittest.TestCase):
    """Pin the exact distinctions required by this conditional-evidence study."""

    def test_size_balance_and_supported_history(self):
        """Role evidence is ordinary reported arguments, not test-label annotations."""
        data = cases()
        self.assertEqual(len(data), 36)
        self.assertEqual(Counter(c["expected"] for c in data), {"allow": 18, "deny": 18})
        self.assertEqual(Counter(c["cohort"] for c in data), {"role": 24, "control": 12})
        for c in data:
            self.assertEqual(any(c["expected_rules"].values()), c["expected"] == "deny")
            self.assertEqual(
                c["context"]["retained_context"]["latest_user_request"], c["agent_prompt"][:800]
            )
            for record in c["context"]["recent_tool_records"]:
                words = shlex.split(record["arguments"]["command"])
                if words[:2] == ["printf", "%s"]:
                    self.assertIsInstance(json.loads(words[2]), dict)
                self.assertEqual(record["tool_name"], "terminal")
                self.assertNotIn("result", record)

    def test_opposite_roles_share_http_and_differ_only_in_arguments(self):
        """Counterfactual role pairs change evidence, not the prompt or outgoing value."""
        families = defaultdict(dict)
        for c in cases():
            if c["cohort"] == "role":
                families[c["pair_id"]][c["variant"]] = c
        self.assertEqual(len(families), 6)
        for family in families.values():
            secret, reference, exempt = [
                family[k] for k in ("credential_role", "ordinary_role", "exemption")
            ]
            self.assertEqual(secret["http"], reference["http"])
            self.assertEqual(secret["http"], exempt["http"])
            self.assertEqual(secret["agent_prompt"], reference["agent_prompt"])
            a, b = copy.deepcopy(secret["context"]), copy.deepcopy(reference["context"])
            for context in (a, b):
                for record in context["recent_tool_records"]:
                    record.pop("arguments")
            self.assertEqual(a, b)
            self.assertNotEqual(secret["expected"], reference["expected"])

    def test_question_candidate_is_exact_and_does_not_change_criteria(self):
        """One frozen U paragraph is the only question change; thresholds stay fixed."""
        catalogs = json.loads((study.ROOT / "questions.json").read_text())["catalogs"]
        originals = json.loads((study.PILOT / "questions.json").read_text())["catalogs"]["baseline"]
        for original, guided, previous in zip(
            catalogs["baseline"]["rules"],
            catalogs["role_guided"]["rules"],
            originals["rules"],
            strict=True,
        ):
            self.assertEqual(original, previous)
            self.assertEqual(guided["question"], build_cases.PREFIX + " " + original["question"])
            self.assertEqual(
                {k: v for k, v in guided.items() if k != "question"},
                {k: v for k, v in original.items() if k != "question"},
            )
            self.assertLessEqual(len(guided["question"].encode()), 1024)

    def test_prepared_request_invariance_and_intentional_sharing(self):
        """108 cells reference96 actual inputs; shared answers never gain fake independence."""
        with tempfile.TemporaryDirectory() as tmp:
            output = prepare_fixture(Path(tmp))
            matrix = json.loads((output / "matrix.json").read_text())
            order = json.loads((output / "call_order.json").read_text())
            self.assertEqual(len(matrix), 108)
            self.assertEqual(len(order), 96)
            usage = Counter(c["request_sha256"] for c in matrix)
            self.assertEqual(Counter(usage.values()), {1: 90, 3: 6})
            self.assertEqual(
                Counter(c["policy"] for c in order),
                {"baseline": 36, "role_guided": 36, "http_only": 24},
            )
            for i in range(0, len(matrix), 3):
                group = {
                    c["policy"]: json.loads(
                        (output / "requests" / (c["request_sha256"] + ".json")).read_text()
                    )
                    for c in matrix[i : i + 3]
                }
                original, guided, plain = [group[p] for p in study.CONDITIONS]
                self.assertEqual(original["state"], guided["state"])
                stripped = copy.deepcopy(original["state"])
                stripped.pop("conversation_context")
                self.assertEqual(stripped, plain["state"])
                self.assertEqual(original["questions"], plain["questions"])
                for request in group.values():
                    self.assertFalse(
                        set(request["state"])
                        & {"expected", "expected_rules", "label_basis", "cohort", "variant"}
                    )
                    self.assertNotIn("expected_role", json.dumps(request))

    def test_actual_block_order_is_balanced_without_claiming_logical_triads(self):
        """Context order is balanced per subtype and controls use all6 permutations twice."""
        with tempfile.TemporaryDirectory() as tmp:
            output = prepare_fixture(Path(tmp))
            order = json.loads((output / "call_order.json").read_text())
            matrix = json.loads((output / "matrix.json").read_text())
            positions = {c["request_sha256"]: i for i, c in enumerate(order)}
            by_case = defaultdict(dict)
            for cell in matrix:
                by_case[cell["case_id"]][cell["policy"]] = cell
            first = Counter()
            for item in by_case.values():
                a, b = item["baseline"], item["role_guided"]
                before = positions[a["request_sha256"]] < positions[b["request_sha256"]]
                first[(a["variant"], before)] += 1
                if a["cohort"] == "role":
                    self.assertEqual(
                        abs(positions[a["request_sha256"]] - positions[b["request_sha256"]]), 1
                    )
            for variant in ("credential_role", "ordinary_role", "different_value", "exemption"):
                self.assertEqual(first[(variant, True)], 3)
                self.assertEqual(first[(variant, False)], 3)
            blocks = defaultdict(list)
            for call in order:
                blocks[call["block_id"]].append(call["policy"])
            permutations = Counter(tuple(v) for v in blocks.values() if len(v) == 3)
            self.assertEqual(len(permutations), 6)
            self.assertEqual(set(permutations.values()), {2})

    def test_interruption_is_durable_and_rerun_is_refused(self):
        """Unknown in-flight assessments remain recorded and cannot be silently retried."""
        with tempfile.TemporaryDirectory() as tmp:
            output = prepare_fixture(Path(tmp))
            with mock.patch.dict(study.os.environ, {"JEV_API_KEY": "nonworking-test-value"}):
                with mock.patch.object(study.helpers, "assess", side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        study.execute(output)
                events = (output / "attempts.jsonl").read_text().splitlines()
                self.assertEqual(len(events), 1)
                self.assertEqual(json.loads(events[0])["event"], "started")
                with self.assertRaises(ValueError):
                    study.execute(output)

    def test_execution_submits_only96_unique_requests(self):
        """A reused HTTP response supports several labels without another network call."""

        def answer(request, key, timeout):
            self.assertEqual(key, "nonworking-test-value")
            measurement = {
                "request_sha256": study.helpers.digest(request),
                "status": "ok",
                "scores": {q: 0.7 for q in request["questions"]},
                "latency_ms": 1,
                "usage": {"input_tokens": 10, "output_tokens": 2},
            }
            return measurement, {"model": "jev-1.14.0"}

        with tempfile.TemporaryDirectory() as tmp:
            output = prepare_fixture(Path(tmp))
            with mock.patch.dict(study.os.environ, {"JEV_API_KEY": "nonworking-test-value"}):
                with mock.patch.object(study.helpers, "assess", side_effect=answer) as api:
                    with redirect_stdout(io.StringIO()):
                        study.execute(output)
            self.assertEqual(api.call_count, 96)
            results = json.loads((output / "results.json").read_text())
            self.assertEqual(len(results["unique_measurements"]), 96)
            self.assertEqual(len(results["cells"]), 108)
            self.assertTrue(all(c["decision"] == "deny" for c in results["cells"]))
            self.assertEqual(
                sum(m["usage"]["input_tokens"] for m in results["unique_measurements"]), 960
            )


if __name__ == "__main__":
    unittest.main()
