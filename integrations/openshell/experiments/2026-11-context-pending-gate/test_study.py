# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Offline lifecycle, exact-input, deduplication and fail-stop contracts."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import study


class GateTests(unittest.TestCase):
    def test_lifecycle_and_gate_semantics(self):
        expected = {
            "history_only": (False, False),
            "sender_pending": (True, True),
            "sender_completed": (False, False),
            "pending_with_newer_completion": (True, False),
            "unrelated_pending": (True, True),
            "completed_with_old_pending": (False, False),
        }
        for _, original in study.bases():
            for variant, (any_pending, newest_pending) in expected.items():
                with self.subTest(variant=variant):
                    request = study.lifecycle(original, variant)
                    self.assertEqual(
                        study.gates(request["state"]["conversation_context"]),
                        {
                            "any_pending": any_pending,
                            "newest_pending": newest_pending,
                        },
                    )
                    plain = copy.deepcopy(request)
                    plain["state"].pop("conversation_context")
                    original_plain = copy.deepcopy(original)
                    original_plain["state"].pop("conversation_context")
                    self.assertEqual(plain, original_plain)
            pending = study.lifecycle(original, "sender_pending")
            completed = study.lifecycle(original, "sender_completed")
            event = completed["state"]["conversation_context"]["model_requests"][0]["tool_calls"][
                0
            ]["events_newest_first"][0]
            event["reported_execution_state"] = "execution_pending"
            self.assertEqual(pending, completed)

    def test_missing_id_and_duplicate_recency(self):
        request = study.lifecycle(study.bases()[0][1], "sender_pending")
        context = request["state"]["conversation_context"]
        call = context["model_requests"][0]["tool_calls"][0]
        call["tool_call_id"] = None
        self.assertEqual(study.gates(context), {"any_pending": False, "newest_pending": False})
        context["model_requests"][0]["tool_calls"][1]["events_newest_first"][0]["recency_rank"] = 1
        with self.assertRaises(ValueError):
            study.gates(context)

    def test_frozen_conditions_and_call_sharing(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "run"
            study.prepare(output)
            matrix = json.loads((output / "matrix.json").read_text())
            calls = json.loads((output / "call_order.json").read_text())
            self.assertEqual(len(matrix), 144)
            self.assertEqual(len(calls), 41)
            groups = {}
            for cell in matrix:
                groups.setdefault((cell["base_case_id"], cell["variant"]), {})[cell["arm"]] = cell
            for arms in groups.values():
                for gate in ("any_pending", "newest_pending"):
                    selected = "ungated_context" if arms[gate]["context_included"] else "http_only"
                    self.assertEqual(arms[gate]["request_sha256"], arms[selected]["request_sha256"])
            with self.assertRaises(ValueError):
                study.prepare(output)

    def test_failed_attempt_cannot_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "run"
            study.prepare(output)

            def fail(request, key, timeout):
                return {
                    "request_sha256": study.helpers.digest(request),
                    "status": "transport_error",
                }, "unavailable"

            with (
                mock.patch.dict(
                    study.os.environ, {"JEV_API_KEY": "nonworking-test-value"}
                ),  # pragma: allowlist secret
                mock.patch.object(study.helpers, "assess", side_effect=fail) as assess,
            ):
                with self.assertRaisesRegex(RuntimeError, "incomplete run"):
                    study.execute(output)
                assess.assert_called_once()
                with self.assertRaises(ValueError):
                    study.execute(output)
            self.assertEqual(len((output / "attempts.jsonl").read_text().splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
