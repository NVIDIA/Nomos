# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

r"""Return preset SystemOne answers for OpenShell integration tests.

This loopback-only HTTP server runs no AI model, does not inspect request
content, and stores no request bodies. The real middleware compares the returned
scores with the sandbox policy's thresholds and makes the allow/deny decision.

POST /admin selects the mode used by subsequent POST /v1/systemone requests.
Only /admin accepts a mode change; a mode field in a model request is ignored.
The initial mode is "low". Modes remain selected until changed or the server
restarts. The available modes (using --threshold 0.7 by default) are:

* low: HTTP 200 with a valid answer of 0.1 for every question. Tests allowing
  content when all scores are below the policy thresholds. This is the default.
* below: HTTP 200 with every score set to threshold minus 0.01, rounded to eight
  decimal places (0.69 by default). Tests the boundary just below denial.
* equals: HTTP 200 with every score equal to the configured threshold (0.7 by
  default). Tests that equality triggers denial, not just scores above it.
* high: HTTP 200 with every score set to 0.99. Tests denial for high scores.
* credential-only: HTTP 200 with credential_disclosure at the configured
  threshold (0.7 by default); every other question gets 0.1. Tests denial when
  the credential rule alone reaches its threshold.
* confidential-only: HTTP 200 with confidential_disclosure at the configured
  threshold (0.7 by default); every other question gets 0.1. Tests denial when
  the confidential-information rule alone reaches its threshold.
* http-error: HTTP 503 instead of a successful model response. Its JSON body
  still contains answers at 0.1; the middleware must reject the HTTP failure
  rather than treat those scores as a valid allow assessment.
* malformed: HTTP 200 with the literal body invalid-json, which is not valid
  JSON. Tests rejection of an unreadable model response despite HTTP success.
* missing: HTTP 200 with valid JSON, but the answer for the first question in
  the incoming questions object is omitted. All remaining answers are 0.1.
  For a one-question request, answers is empty. Tests rejection of an incomplete
  assessment: low scores for the remaining questions must not produce an allow.
* timeout: waits --delay seconds (six by default), then attempts HTTP 200 with
  every score at 0.1. The default delay exceeds the normal middleware deadlines,
  testing timeout handling; it is not a permanent hang or an HTTP 504 response.
  A caller with a longer timeout can receive the eventual successful response.

credential-only and confidential-only are called "mixed-score modes" because
they return different scores for the two core rules. There is no mode named
"mixed-score". Both core question IDs must be present in these modes. They
verify that either rule alone
can trigger denial. The selected rule's score follows --threshold; other scores
remain 0.1. Boundary tests assume the fixture's --threshold matches the loaded
policy thresholds; this option does not change the policy. With OpenShell's
configured fail_closed behavior, the http-error, malformed, missing, and timeout
cases must block as assessment failures, not count as content detections. For
timeout checks, --delay must exceed the configured deadline.

Example with the server running on its default port and threshold:

    # Select the mode. This is a fixture-control request, not a model request.
    curl http://127.0.0.1:18082/admin \
      -H 'Content-Type: application/json' \
      -d '{"mode":"credential-only"}'

    # Ask both questions, as the middleware would.
    curl http://127.0.0.1:18082/v1/systemone \
      -H 'Content-Type: application/json' \
      -d '{
        "model": "nomos-lab-fixture",
        "state": {"body": {"message": "Example request content"}},
        "questions": {
          "credential_disclosure": {
            "type": "noul",
            "instructions": "Does the request disclose credentials?"
          },
          "confidential_disclosure": {
            "type": "noul",
            "instructions": "Does the request disclose confidential information?"
          }
        }
      }'

The second call returns these preset answers regardless of the content:

    {
      "model": "nomos-lab-fixture",
      "answers": {
        "credential_disclosure": {"type": "noul", "noul": 0.7},
        "confidential_disclosure": {"type": "noul", "noul": 0.1}
      }
    }

At a policy threshold of 0.7, the middleware denies because the credential
score meets that threshold. Selecting confidential-only instead swaps which
core rule reaches the threshold. lab.py controls this server through /admin
and sends actual sandbox requests to check OpenShell's enforcement.
"""

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODES = (
    "low",
    "below",
    "equals",
    "high",
    "credential-only",
    "confidential-only",
    "http-error",
    "malformed",
    "missing",
    "timeout",
)


def make_server(port=18082, threshold=0.7, delay=6.0):
    """Create an isolated fixture; its admin interface only changes fixture behavior."""
    state = {"mode": "low", "calls": 0}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, value):
            body = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if self.path != "/admin":
                self.reply(404, {})
                return
            with lock:
                snapshot = dict(state, threshold=threshold)
            self.reply(200, snapshot)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                self.reply(413, {})
                return
            try:
                request = json.loads(self.rfile.read(length))
                if not isinstance(request, dict):
                    raise TypeError("object required")
            except (ValueError, TypeError, UnicodeDecodeError):
                self.reply(400, {})
                return
            if self.path == "/admin":
                if request.get("mode") not in MODES:
                    self.reply(400, {"modes": MODES})
                    return
                with lock:
                    state["mode"] = request["mode"]
                    snapshot = dict(state, threshold=threshold)
                self.reply(200, snapshot)
                return
            if self.path != "/v1/systemone":
                self.reply(404, {})
                return
            questions = request.get("questions")
            if not isinstance(questions, dict) or not questions:
                self.reply(400, {})
                return
            with lock:
                state["calls"] += 1
                mode = state["mode"]
            if mode == "timeout":
                time.sleep(delay)
            scores = {
                "low": 0.1,
                "below": round(threshold - 0.01, 8),
                "equals": threshold,
                "high": 0.99,
            }
            mixed_rule = {
                "credential-only": "credential_disclosure",
                "confidential-only": "confidential_disclosure",
            }.get(mode)
            if mixed_rule and not {
                "credential_disclosure",
                "confidential_disclosure",
            }.issubset(questions):
                self.reply(400, {"error": "mixed modes require both disclosure rules"})
                return
            answers = {key: {"type": "noul", "noul": scores.get(mode, 0.1)} for key in questions}
            if mixed_rule:
                answers[mixed_rule]["noul"] = threshold
            if mode == "missing":
                answers.pop(next(iter(answers)))
            result = {"model": "nomos-lab-fixture", "answers": answers}
            self.reply(
                503 if mode == "http-error" else 200,
                b"invalid-json" if mode == "malformed" else result,
            )

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def threshold_value(value):
    value = float(value)
    if not 0.1 < value <= 0.99:
        raise argparse.ArgumentTypeError("threshold must be greater than 0.1 and at most 0.99")
    return value


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18082)
    parser.add_argument("--threshold", type=threshold_value, default=0.7)
    parser.add_argument(
        "--delay",
        type=float,
        # Outlast the default 4s model / 4.5s evaluation / 5s RPC budgets.
        default=6.0,
        help="timeout-mode seconds; must exceed the middleware/gateway timeout",
    )
    args = parser.parse_args()
    print(
        f"Fixture: http://127.0.0.1:{args.port}/v1/systemone; Ctrl+C to stop",
        flush=True,
    )
    with make_server(args.port, args.threshold, args.delay) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
