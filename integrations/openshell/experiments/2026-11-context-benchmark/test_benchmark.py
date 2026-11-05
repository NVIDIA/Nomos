# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Offline contracts for normal-path lifecycle, controlled contrasts and fail-stop replay."""

import contextlib
import copy
import io
import json
import shlex
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

import benchmark
import context as context_format


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        """Keep mocked preparation/failure output from resembling a live run."""
        capture = contextlib.redirect_stdout(io.StringIO())
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def test_dataset_counts_and_pending_sender(self):
        cases = json.loads((benchmark.ROOT / "fixtures.json").read_text())["cases"]
        self.assertEqual(len(cases), 132)
        self.assertEqual(len({c["id"] for c in cases}), 132)
        groups = Counter(
            c["cohort"] if c["study_cohort"] == "role_study" else "distraction" for c in cases
        )
        self.assertEqual(groups, {"role": 24, "control": 12, "distraction": 96})
        for case in cases:
            with self.subTest(case=case["id"]):
                state = context_format.make_state(case)
                self.assertEqual({k: state[k] for k in case["http"]}, case["http"])
                context = state["conversation_context"]
                self.assertTrue(context_format.has_pending_call(context))
                current = context["model_requests"][0]
                self.assertEqual(current["relationship"], "current retained model request")
                call = current["tool_calls"][0]
                event = call["events_newest_first"][0]
                self.assertEqual(event["reported_execution_state"], "execution_pending")
                self.assertEqual(event["recency_rank"], 1)
                argv = shlex.split(event["arguments"]["command"])
                self.assertEqual(json.loads(argv[argv.index("--data-binary") + 1]), state["body"])
                self.assertEqual(argv[argv.index("--request") + 1], state["method"])
                self.assertEqual(argv[argv.index("--header") + 1], "Content-Type: application/json")
                expected_url = f"{state['scheme']}://{state['host']}:{state['port']}{state['path']}"
                if state["query"]:
                    expected_url += "?" + state["query"]
                self.assertEqual(argv[-1], expected_url)
                history = copy.deepcopy(context["model_requests"][1:])
                original = context_format.normalized_context(case["context"])["model_requests"]
                for group in original:
                    if group["relationship"] == "current retained model request":
                        group["relationship"] = (
                            "earlier model request; its retained user text is not supplied"
                        )
                    for old_call in group["tool_calls"]:
                        self.assertNotEqual(call["tool_call_id"], old_call["tool_call_id"])
                        for old_event in old_call["events_newest_first"]:
                            old_event["recency_rank"] += 1
                self.assertEqual(history, original)
                self.assertEqual(
                    context["retained_user_request"],
                    case["context"]["retained_context"]["latest_user_request"],
                )

    def test_latest_event_per_id_and_completed_history(self):
        context = {
            "model_requests": [
                {
                    "tool_calls": [
                        {
                            "tool_call_id": "a",
                            "events_newest_first": [
                                {"recency_rank": 1, "reported_execution_state": "completed"},
                                {
                                    "recency_rank": 3,
                                    "reported_execution_state": "execution_pending",
                                },
                            ],
                        },
                        {
                            "tool_call_id": "b",
                            "events_newest_first": [
                                {
                                    "recency_rank": 2,
                                    "reported_execution_state": "execution_pending",
                                },
                            ],
                        },
                    ]
                }
            ]
        }
        self.assertTrue(context_format.has_pending_call(context))
        context["model_requests"][0]["tool_calls"].pop()
        self.assertFalse(context_format.has_pending_call(context))

    def test_condition_differences_and_global_deduplication(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "run"
            benchmark.prepare(output)
            matrix = json.loads((output / "matrix.json").read_text())
            calls = json.loads((output / "call_order.json").read_text())
            self.assertEqual(len(matrix), 528)
            self.assertEqual(len(calls), 516)
            self.assertEqual(
                {c["request_sha256"] for c in matrix}, {c["request_sha256"] for c in calls}
            )
            self.assertEqual(len(calls), len({c["request_sha256"] for c in calls}))
            for start in range(0, len(matrix), 4):
                inputs = {
                    c["arm"]: json.loads(
                        (output / "requests" / (c["request_sha256"] + ".json")).read_text()
                    )
                    for c in matrix[start : start + 4]
                }
                original, guided, ablated, plain = [inputs[a] for a in benchmark.ARMS]
                self.assertEqual(original["state"], guided["state"])
                self.assertEqual(original["questions"], ablated["questions"])
                self.assertEqual(original["questions"], plain["questions"])
                for rule, question in original["questions"].items():
                    self.assertTrue(
                        guided["questions"][rule]["instructions"].endswith(question["instructions"])
                    )
                expected = copy.deepcopy(original["state"])
                expected.pop("conversation_context")
                self.assertEqual(expected, plain["state"])
                expected = copy.deepcopy(original["state"])
                for group in expected["conversation_context"]["model_requests"]:
                    for call in group["tool_calls"]:
                        for event in call["events_newest_first"]:
                            event.pop("nomos_council", None)
                self.assertEqual(expected, ablated["state"])
            with self.assertRaises(ValueError):
                benchmark.prepare(output)

    def test_failed_attempt_is_durable_and_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "run"
            benchmark.prepare(output)

            def fail(request, key, timeout):
                return {
                    "request_sha256": benchmark.model_io.digest(request),
                    "request_bytes": len(benchmark.model_io.encoded(request)),
                    "status": "transport_error",
                }, "Unavailable; no retry"

            with (
                mock.patch.dict(
                    benchmark.os.environ, {"JEV_API_KEY": "nonworking-test-value"}
                ),  # pragma: allowlist secret
                mock.patch.object(benchmark.model_io, "assess", side_effect=fail) as assess,
            ):
                with self.assertRaisesRegex(RuntimeError, "incomplete benchmark"):
                    benchmark.execute(output)
                assess.assert_called_once()
                with self.assertRaises(ValueError):
                    benchmark.execute(output)
            result = json.loads((output / "results.json").read_text())
            self.assertTrue(all(c["status"] != "ok" for c in result["cells"]))
            self.assertEqual(len((output / "attempts.jsonl").read_text().splitlines()), 2)

    def test_changed_source_or_request_fails_before_model_call(self):
        for change in ("source", "request"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / "run"
                benchmark.prepare(output)
                if change == "source":
                    manifest = json.loads((output / "manifest.json").read_text())
                    manifest["source_hashes"]["PLAN.md"] = "changed"
                    benchmark.model_io.write_json(output / "manifest.json", manifest)
                else:
                    first = json.loads((output / "call_order.json").read_text())[0]
                    (output / "requests" / (first["request_sha256"] + ".json")).write_text("{}\n")
                with (
                    mock.patch.dict(
                        benchmark.os.environ, {"JEV_API_KEY": "nonworking-test-value"}
                    ),  # pragma: allowlist secret
                    mock.patch.object(benchmark.model_io, "assess") as assess,
                ):
                    with self.assertRaises(ValueError):
                        benchmark.execute(output)
                    assess.assert_not_called()

    def test_inclusive_threshold_and_invalid_model_scores(self):
        request = {"model": benchmark.MODEL, "questions": {"credential_disclosure": {}}}
        response = {
            "model": benchmark.MODEL,
            "answers": {"credential_disclosure": {"type": "noul", "noul": 0.70}},
        }
        self.assertEqual(
            benchmark.model_io.parse_scores(response, request["model"], list(request["questions"])),
            {"credential_disclosure": 0.70},
        )
        for invalid in (True, float("nan"), 1.1, -0.1):
            response["answers"]["credential_disclosure"]["noul"] = invalid
            with self.assertRaises(ValueError):
                benchmark.model_io.parse_scores(
                    response, request["model"], list(request["questions"])
                )


if __name__ == "__main__":
    unittest.main()
