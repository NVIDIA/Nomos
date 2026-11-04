# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Offline contracts: fail on wrong decisions, forwarding, evidence or settings."""

import base64
import copy
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

import demo

# Deliberately nonfunctional URL used to verify credential rejection.
REJECTED_URL = "http://user:placeholder@localhost:18081"  # pragma: allowlist secret


def evidence(name="public"):
    """Construct independent transport/log evidence for one synthetic request."""
    case = demo.CASES[name]
    payload = json.dumps(case["body"], separators=(",", ":"), ensure_ascii=False).encode()
    digest = hashlib.sha256(payload).hexdigest()
    before = {"received_count": 7, "requests": []}
    after = {"received_count": 7, "requests": []}
    if case["outcome"] == "allow":
        after = {
            "received_count": 8,
            "requests": [
                {
                    "request_id": 8,
                    "method": "POST",
                    "path": "/publish",
                    "body_base64": base64.b64encode(payload).decode(),
                    "sha256": digest,
                }
            ],
        }
    events = []
    if case["outcome"] is not None:
        fields = [
            {"message": "review_input_validated", "body_sha256": digest},
            {"message": "model_call_started"},
            {
                "message": "model_call_finished",
                "model_outcome": "complete",
                "model_completion_observed": True,
            },
            {"message": "review_complete", "outcome": case["outcome"]},
        ]
        events = [{"fields": field, "span": {"request_id": "request-demo"}} for field in fields]
    response = {} if case["error"] is None else {"error": case["error"]}
    return [name, payload, case["status"], response, before, after, events]


class EvidenceTests(unittest.TestCase):
    """A passing CLI exit or 403 alone cannot count as a successful demo."""

    def test_all_four_expected_paths(self):
        """Accept intact forwarding, model denials and independent policy denial."""
        for name in demo.CASES:
            with self.subTest(name=name):
                demo.verify_evidence(*evidence(name))

    def test_wrong_response_or_outcome(self):
        """Reject infrastructure failures being presented as content denials."""
        for change in ("status", "error", "outcome", "model_failure"):
            args = evidence("credential")
            if change == "status":
                args[2] = 200
            elif change == "error":
                args[3] = {"error": "middleware_failed"}
            elif change == "outcome":
                args[-1][-1]["fields"]["outcome"] = "allow"
            else:
                args[-1][-2]["fields"]["model_outcome"] = "timeout"
            with self.subTest(change=change), self.assertRaises(demo.DemoError):
                demo.verify_evidence(*args)

    def test_content_denial_must_not_forward(self):
        """A rejected request arriving at the destination is always a failure."""
        args = evidence("credential")
        args[-2]["received_count"] += 1
        with self.assertRaises(demo.DemoError):
            demo.verify_evidence(*args)

    def test_allow_must_preserve_body_method_path_and_hash(self):
        """Check actual bytes even if a changed body carries a consistent hash."""
        for field in ("body_base64", "method", "path", "sha256", "received_count"):
            args = evidence()
            record = args[-2]["requests"][0]
            if field == "body_base64":
                record[field] = base64.b64encode(b'{"message":"changed"}').decode()
                record["sha256"] = hashlib.sha256(b'{"message":"changed"}').hexdigest()
            elif field == "received_count":
                args[-2][field] = args[-3][field]
            else:
                record[field] = "changed"
            with self.subTest(field=field), self.assertRaises(demo.DemoError):
                demo.verify_evidence(*args)

    def test_missing_extra_or_unrelated_log_evidence(self):
        """Reject missing calls, concurrent calls and another request's events."""
        for change in ("missing", "extra", "body_hash", "request_id"):
            args = evidence()
            if change == "missing":
                args[-1].pop(-2)
            elif change == "extra":
                args[-1].append(copy.deepcopy(args[-1][-2]))
            elif change == "body_hash":
                args[-1][0]["fields"]["body_sha256"] = "wrong"
            else:
                args[-1][-1]["span"]["request_id"] = "other-request"
            with self.subTest(change=change), self.assertRaises(demo.DemoError):
                demo.verify_evidence(*args)

    def test_policy_denial_must_not_call_nomos(self):
        """Even an incomplete Nomos attempt invalidates the policy-only claim."""
        args = evidence("network-policy")
        args[-1] = [{"fields": {"message": "model_call_started"}}]
        with self.assertRaises(demo.DemoError):
            demo.verify_evidence(*args)


class LogDeliveryTests(unittest.TestCase):
    """Allow bounded tee delivery without resending the HTTP or model request."""

    def test_delayed_terminal_event_is_collected(self):
        """A log writer catching up is not a failed model assessment."""
        events = evidence()[-1]
        with (
            mock.patch.object(demo, "log_events", side_effect=[[], events]),
            mock.patch.object(demo.time, "monotonic", side_effect=[0, 0.1]),
            mock.patch.object(demo.time, "sleep"),
        ):
            self.assertEqual(demo.collect_request_events(None, 0, True), events)

    def test_missing_events_stop_at_deadline(self):
        """Do not wait indefinitely for absent evidence or retry any request."""
        with (
            mock.patch.object(demo, "log_events", return_value=[]),
            mock.patch.object(demo.time, "monotonic", side_effect=[0, 1.1]),
        ):
            self.assertEqual(demo.collect_request_events(None, 0, True), [])

    def test_policy_denial_collects_delayed_middleware_events(self):
        """Late middleware activity must invalidate the policy-only result."""
        events = [{"fields": {"message": "model_call_started"}}]
        with (
            mock.patch.object(demo, "log_events", side_effect=[[], events]) as read,
            mock.patch.object(demo.time, "monotonic", side_effect=[0, 0.1]),
            mock.patch.object(demo.time, "sleep") as sleep,
        ):
            collected = demo.collect_request_events(None, 0, False)
        self.assertEqual(read.call_count, 2)
        sleep.assert_called_once()
        args = evidence("network-policy")
        args[-1] = collected
        with self.assertRaisesRegex(demo.DemoError, "middleware events appeared"):
            demo.verify_evidence(*args)

    def test_policy_denial_waits_until_deadline_without_events(self):
        """An empty snapshot only passes after the bounded delivery window."""
        with (
            mock.patch.object(demo, "log_events", return_value=[]) as read,
            mock.patch.object(demo.time, "monotonic", side_effect=[0, 0.1, 1.0]),
            mock.patch.object(demo.time, "sleep") as sleep,
        ):
            collected = demo.collect_request_events(None, 0, False)
        self.assertEqual(read.call_count, 2)
        sleep.assert_called_once()
        args = evidence("network-policy")
        args[-1] = collected
        demo.verify_evidence(*args)

    def test_partial_log_line_waits_for_its_newline(self):
        """Read complete JSONL records only; a tee write can be observed midway."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "middleware.log"
            path.write_text('{"fields":{"message":"first"}}\n{"fields":')
            settings = demo.DemoSettings(
                "demo", "demo", "http://localhost", "http://localhost", path, Path(tmp)
            )
            self.assertEqual(len(demo.log_events(settings)), 1)
            with path.open("a") as log:
                log.write('{"message":"second"}}\n')
            self.assertEqual(len(demo.log_events(settings)), 2)


class CurlResponseTests(unittest.TestCase):
    """Curl success alone must not turn malformed output into a demo result."""

    def test_valid_http_responses(self):
        """Accept success and denial objects for supported HTTP status formats."""
        for version, status in ((b"1.1", 200), (b"2", 403), (b"3", 503)):
            with self.subTest(version=version, status=status):
                output = (
                    b"HTTP/"
                    + version
                    + f" {status}\r\ncontent-type: application/json\r\n\r\n".encode()
                    + b'{"ok":true}'
                )
                result = subprocess.CompletedProcess([], 0, output, b"")
                self.assertEqual(demo.parse_curl_response("public", result), (status, {"ok": True}))

    def test_nonzero_exit_reports_step_and_bounded_stderr(self):
        """Preserve useful command diagnostics without dumping unlimited output."""
        result = subprocess.CompletedProcess([], 7, b"", b"connection failed\xff" + b"x" * 1000)
        with self.assertRaises(demo.DemoError) as caught:
            demo.parse_curl_response("public", result)
        message = str(caught.exception)
        self.assertIn("public: curl command exited 7: connection failed", message)
        self.assertIn("\ufffd", message)
        self.assertLess(len(message), 600)

    def test_malformed_http_response(self):
        """Reject missing framing and invalid status lines with contextual errors."""
        for output in (
            b"",
            b"HTTP/1.1",
            b"HTTP/1.1 200 OK",
            b"HTTP/1.1\r\n\r\n{}",
            b"HTTP/1.1 nope\r\n\r\n{}",
            b"HTTP/1.1 999 Invalid\r\n\r\n{}",
            b"not-http 200 OK\r\n\r\n{}",
        ):
            with (
                self.subTest(output=output),
                self.assertRaisesRegex(demo.DemoError, "credential: malformed HTTP"),
            ):
                demo.parse_curl_response(
                    "credential", subprocess.CompletedProcess([], 0, output, b"")
                )

    def test_body_must_be_a_json_object(self):
        """HTML, invalid encoding and JSON scalar/array bodies cannot pass."""
        for body in (b"<html>error</html>", b"\xff", b"[]", b"null", b'"text"'):
            with (
                self.subTest(body=body),
                self.assertRaisesRegex(demo.DemoError, "public: response body is not"),
            ):
                demo.parse_curl_response(
                    "public",
                    subprocess.CompletedProcess([], 0, b"HTTP/1.1 200 OK\r\n\r\n" + body, b""),
                )

    def test_demonstrate_stops_after_command_failure(self):
        """A failed sandbox command must not collect evidence or print PASS."""
        settings = demo.DemoSettings(
            "demo", "demo", "http://localhost", "http://localhost", Path("unused"), Path(".")
        )
        result = subprocess.CompletedProcess([], 7, b"", b"connection failed")
        with (
            mock.patch.object(
                demo, "destination_records", return_value={"received_count": 0}
            ) as destination,
            mock.patch.object(demo, "log_events", return_value=[]),
            mock.patch.object(demo, "collect_request_events") as events,
            mock.patch.object(demo.subprocess, "run", return_value=result) as run,
            mock.patch("builtins.print") as output,
        ):
            with self.assertRaisesRegex(demo.DemoError, "public: curl command exited 7"):
                demo.demonstrate("public", settings)
            destination.assert_called_once_with(settings)
            events.assert_not_called()
            run.assert_called_once()
            self.assertEqual(run.call_args.kwargs["timeout"], 25)
            self.assertFalse(run.call_args.kwargs["check"])
            self.assertNotIn(mock.call("PASS"), output.call_args_list)


class SettingsTests(unittest.TestCase):
    """Local configuration must not accidentally capture credentials in results."""

    def test_platform_examples(self):
        """Both commented YAML examples load with the intended host address."""
        for name, destination in (
            ("demo-config.example.yaml", "http://BRIDGE_ADDRESS:18081"),
            ("demo-config.macos.example.yaml", "http://127.0.0.1:18081"),
        ):
            with self.subTest(name=name):
                settings = demo.load_settings(Path(__file__).parent / name)
                self.assertEqual(settings.destination_url, destination)
                self.assertEqual(settings.gateway, "review-lab")

    def test_reject_malformed_and_unsafe_yaml(self):
        """Malformed YAML and Python object tags must raise a settings error."""
        for content in ("gateway: [", "!!python/object:builtins.object {}"):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "deployment.yaml"
                path.write_text(content)
                with self.assertRaisesRegex(demo.DemoError, "Cannot read demo config"):
                    demo.load_settings(path)

    def test_relative_log_path_uses_config_directory(self):
        """Resolve relative logs and outputs beside the runtime config."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            config = {
                "gateway": "demo",
                "sandbox": "demo",
                "destination_url": "http://127.0.0.1:18081",
                "sandbox_destination_url": "http://host.openshell.internal:18081",
                "middleware_log": "middleware.log",
            }
            path = root / "deployment.yaml"
            path.write_text("# Demo settings support comments.\n" + yaml.safe_dump(config))
            settings = demo.load_settings(path)
            self.assertEqual(settings.middleware_log, root / "middleware.log")
            self.assertEqual(settings.output_dir, root)

    def test_reject_secret_fields_and_url_credentials(self):
        """Do not serialize unexpected API-key fields or credential-bearing URLs."""
        base = {
            "gateway": "demo",
            "sandbox": "demo",
            "destination_url": "http://127.0.0.1:18081",
            "sandbox_destination_url": "http://host.openshell.internal:18081",
        }
        for change in (
            {"api_key": "not-a-real-key"},  # pragma: allowlist secret
            {"destination_url": REJECTED_URL},
        ):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                p = Path(tmp) / "deployment.yaml"
                p.write_text(yaml.safe_dump({**base, **change}))
                with self.assertRaises(demo.DemoError):
                    demo.load_settings(p)

    def test_reject_invalid_settings(self):
        """Exercise URL boundaries, nonempty strings, required keys and object shape."""
        base = {
            "gateway": "demo",
            "sandbox": "demo",
            "destination_url": "http://127.0.0.1:18081",
            "sandbox_destination_url": "http://host.openshell.internal:18081",
        }
        cases = []
        for field in ("destination_url", "sandbox_destination_url"):
            for value in (
                "file:///etc/passwd",
                "http:///",
                "http://localhost:18081/requests",
                "http://localhost:18081?x=1",
                "http://localhost:18081#frag",
            ):
                cases.append((f"{field}: {value}", {**base, field: value}))
        for field in (*base, "middleware_log"):
            for value in ("  ", 123, None):
                cases.append((f"{field}: {value!r}", {**base, field: value}))
        for field in base:
            cases.append((f"missing {field}", {k: v for k, v in base.items() if k != field}))
        cases.append(("non-object document", []))
        for name, config in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "deployment.yaml"
                path.write_text("# Demo settings support comments.\n" + yaml.safe_dump(config))
                with self.assertRaises(demo.DemoError):
                    demo.load_settings(path)


if __name__ == "__main__":
    unittest.main()
