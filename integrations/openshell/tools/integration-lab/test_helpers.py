# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Boundary checks for fixture responses and destination evidence verification."""

import base64
import hashlib
import json
import threading
import unittest
import urllib.error
from types import SimpleNamespace

from lab import Runner, classify_response, http_json, unchanged_body
from mock_decision_model import make_server


class GatewaySelectionTests(unittest.TestCase):
    def test_selected_default_and_explicit_overrides(self):
        for gateway, endpoint, expected in [
            (None, None, ["openshell"]),
            ("other-lab", None, ["openshell", "--gateway", "other-lab"]),
            (
                None,
                "http://127.0.0.1:17670",
                ["openshell", "--gateway-endpoint", "http://127.0.0.1:17670"],
            ),
        ]:
            args = SimpleNamespace(
                openshell="openshell", gateway=gateway, gateway_endpoint=endpoint
            )
            self.assertEqual(Runner(args).cli, expected)


class EvidenceTests(unittest.TestCase):
    def test_service_requests_reject_non_http_schemes_before_io(self):
        """Operator service URLs must not turn fixture access into local file reads."""
        for url in ("file:///tmp/example.json", "ftp://fixture.example/requests"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                http_json(url)

    def test_fail_closed_is_not_content_denial(self):
        for kind in (
            "middleware_failed",
            "middleware_denied",
            "policy_denied",
            "unsupported_l7_protocol",
        ):
            response = b"HTTP/1.1 403 Forbidden\r\nContent-Type: application/json\r\n\r\n"
            response += json.dumps({"error": kind}).encode()
            self.assertEqual(classify_response(response), (403, kind))
        self.assertEqual(classify_response(b"command failed: 403"), (None, "unrecognized_response"))

    def test_body_and_hash_and_arrival_must_agree(self):
        original = b'{ "publication": "public" }'
        before = {"received_count": 4}
        entry = {
            "request_id": 5,
            "body_base64": base64.b64encode(original).decode(),
            "sha256": hashlib.sha256(original).hexdigest(),
            "body_bytes": len(original),
        }
        after = {"received_count": 5, "requests": [entry]}
        self.assertTrue(unchanged_body(before, after, original))
        self.assertFalse(unchanged_body(before, after, b'{"publication":"public"}'))
        entry["sha256"] = "incorrect"
        self.assertFalse(unchanged_body(before, after, original))
        after["received_count"] = 6
        self.assertFalse(unchanged_body(before, after, original))

    def test_same_body_at_wrong_method_or_path_is_not_forwarding_evidence(self):
        body = b'{"message":"fictional"}'
        entry = {
            "request_id": 1,
            "body_base64": base64.b64encode(body).decode(),
            "body_bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
            "method": "PUT",
            "path": "/documents/123",
        }
        before = {"received_count": 0}
        after = {"received_count": 1, "requests": [entry]}
        self.assertTrue(unchanged_body(before, after, body, method="PUT", path="/documents/123"))
        self.assertFalse(unchanged_body(before, after, body, method="POST", path="/documents/123"))
        self.assertFalse(unchanged_body(before, after, body, method="PUT", path="/messages"))
        del entry["path"]
        self.assertFalse(unchanged_body(before, after, body, method="PUT", path="/documents/123"))


class FixtureTests(unittest.TestCase):
    def setUp(self):
        self.server = make_server(port=0)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.request = {"questions": {"credential": {}, "confidential": {}}}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join()

    def test_boundaries_and_call_counter(self):
        for mode, score in (
            ("low", 0.1),
            ("below", 0.69),
            ("equals", 0.7),
            ("high", 0.99),
        ):
            http_json(self.url + "/admin", {"mode": mode})
            response = http_json(self.url + "/v1/systemone", self.request)
            self.assertEqual(response["model"], "nomos-lab-fixture")
            self.assertEqual(set(response["answers"]), set(self.request["questions"]))
            self.assertTrue(
                all(
                    answer == {"type": "noul", "noul": score}
                    for answer in response["answers"].values()
                )
            )
        self.assertEqual(http_json(self.url + "/admin")["calls"], 4)

    def test_each_disclosure_rule_can_reach_threshold_independently(self):
        request = {"questions": {"credential_disclosure": {}, "confidential_disclosure": {}}}
        for mode, expected in (
            (
                "credential-only",
                {"credential_disclosure": 0.7, "confidential_disclosure": 0.1},
            ),
            (
                "confidential-only",
                {"credential_disclosure": 0.1, "confidential_disclosure": 0.7},
            ),
            ("below", {"credential_disclosure": 0.69, "confidential_disclosure": 0.69}),
        ):
            http_json(self.url + "/admin", {"mode": mode})
            response = http_json(self.url + "/v1/systemone", request)
            self.assertEqual(
                {key: answer["noul"] for key, answer in response["answers"].items()},
                expected,
            )
        http_json(self.url + "/admin", {"mode": "credential-only"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            http_json(self.url + "/v1/systemone", self.request)
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()

    def test_incomplete_and_failed_assessments(self):
        http_json(self.url + "/admin", {"mode": "missing"})
        response = http_json(self.url + "/v1/systemone", self.request)
        self.assertEqual(len(response["answers"]), 1)
        http_json(self.url + "/admin", {"mode": "http-error"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            http_json(self.url + "/v1/systemone", self.request)
        self.assertEqual(caught.exception.code, 503)
        caught.exception.close()
        http_json(self.url + "/admin", {"mode": "malformed"})
        with self.assertRaises(ValueError):
            http_json(self.url + "/v1/systemone", self.request)

    def test_invalid_mode_does_not_change_behavior(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            http_json(self.url + "/admin", {"mode": "anything"})
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()
        self.assertEqual(http_json(self.url + "/admin")["mode"], "low")


if __name__ == "__main__":
    unittest.main()
