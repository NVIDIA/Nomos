# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import base64
import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from nomos.config.settings import OPAConfig
from nomos.level_0.types import Intent, PackagedFile
from nomos.opa import (
    OPAHttpClient,
    OPAPolicyDecision,
    build_intent_filter_input,
    parse_opa_decision,
)
from nomos.opa.client import OPAEvaluationError

# One test reaches a loopback server through a client that reads proxy
# settings from the environment; an inherited proxy would be sent the request.
from tests.test_vault_proxy import _no_inherited_proxy as _no_inherited_proxy


def _make_intent() -> Intent:
    script = b"print('reviewed')\n"
    packaged_file = PackagedFile(
        path="scripts/reviewed.py",
        content_base64=base64.b64encode(script).decode("ascii"),
        mode="0755",
        description="reviewed helper",
    )
    return Intent.create(
        agent_id="agent-opa",
        description="Run a packaged reviewed helper",
        target_resource="/workspace",
        command="/usr/local/bin/python3 scripts/reviewed.py",
        working_directory="/workspace",
        justification="Testing OPA input",
        blast_radius="Writes one test artifact",
        packaged_files=[packaged_file],
    )


def test_build_intent_filter_input_excludes_packaged_file_contents():
    intent = _make_intent()

    input_doc = build_intent_filter_input(
        intent,
        request_type="authorize",
        authenticated=True,
        agent_permissions={"shell:write"},
        required_permissions={"shell:write", "file:write"},
    )

    file_info = input_doc["intent"]["packaged_files"][0]
    assert file_info["path"] == "scripts/reviewed.py"
    assert file_info["sha256"] == intent.packaged_files[0].sha256
    assert file_info["size_bytes"] == len(b"print('reviewed')\n")
    assert "content_base64" not in file_info
    assert "cHJpbnQo" not in json.dumps(input_doc)
    assert input_doc["agent"]["permissions"] == ["shell:write"]
    assert input_doc["required_permissions"] == ["file:write", "shell:write"]
    assert input_doc["council"] == {
        "id": intent.council_id,
        "opa_policy_id": "strict-shell",
        "opa_relaxed_bare_command_words": [],
        "opa_relaxed_working_dir_prefixes": [],
    }


def test_build_intent_filter_input_includes_normalized_path_references():
    intent = Intent.create(
        agent_id="agent-opa",
        description="Read a key file",
        target_resource="/app//keys/council.pub",
        command="/bin/cat council.pub",
        working_directory="/app/./keys",
        justification="Testing normalized path references",
        blast_radius="Read-only",
    )

    input_doc = build_intent_filter_input(
        intent,
        request_type="authorize",
        authenticated=True,
    )

    assert "/app/keys" in input_doc["intent"]["normalized_path_refs"]
    assert "/app/keys/council.pub" in input_doc["intent"]["normalized_path_refs"]


def test_build_intent_filter_input_keeps_partially_quoted_glob_active():
    """Quoting is per character: ``"/etc/"shado?`` still expands, and a
    mixed-quoted word elsewhere in the command must not disable detection."""
    for command in ('/bin/cat "/etc/"shado?', '/bin/echo "hello"world; /bin/cat /etc/shado?'):
        intent = Intent.create(
            agent_id="agent-opa",
            description="Read a file by pattern",
            target_resource="/tmp",
            command=command,
            working_directory="/tmp",
            justification="Testing glob references",
            blast_radius="Read-only",
        )
        input_doc = build_intent_filter_input(intent, request_type="authorize", authenticated=True)
        assert input_doc["intent"]["normalized_glob_refs"] == ["/etc/shado?"], command


def test_build_intent_filter_input_joins_line_continuations():
    intent = Intent.create(
        agent_id="agent-opa",
        description="Read a file by pattern",
        target_resource="/tmp",
        command="/bin/grep -r . /app/k\\\ne*",
        working_directory="/tmp",
        justification="Testing glob references",
        blast_radius="Read-only",
    )
    input_doc = build_intent_filter_input(intent, request_type="authorize", authenticated=True)
    assert input_doc["intent"]["normalized_glob_refs"] == ["/app/ke*"]


def test_build_intent_filter_input_resolves_relative_glob_references():
    intent = Intent.create(
        agent_id="agent-opa",
        description="Read a file by pattern",
        target_resource="/etc",
        command="/bin/cat shado?",
        working_directory="/etc",
        justification="Testing glob references",
        blast_radius="Read-only",
    )

    input_doc = build_intent_filter_input(intent, request_type="authorize", authenticated=True)

    assert input_doc["intent"]["normalized_glob_refs"] == ["/etc/shado?"]
    assert "/etc/shado?" not in input_doc["intent"]["normalized_path_refs"]


def test_build_intent_filter_input_keeps_quoted_metacharacters_literal():
    intent = Intent.create(
        agent_id="agent-opa",
        description="Search a file",
        target_resource="/tmp/work",
        command="/bin/grep '.*' README.md",
        working_directory="/tmp/work",
        justification="Testing literal references",
        blast_radius="Read-only",
    )

    input_doc = build_intent_filter_input(intent, request_type="authorize", authenticated=True)

    assert input_doc["intent"]["normalized_glob_refs"] == []
    assert "/tmp/work/.*" in input_doc["intent"]["normalized_path_refs"]


def test_build_intent_filter_input_includes_council_opa_profile():
    intent = _make_intent()
    input_doc = build_intent_filter_input(
        intent,
        request_type="authorize",
        authenticated=True,
        opa_config=OPAConfig(
            policy_id="git-workflow-local",
            relaxed_bare_command_words=["git", "gh"],
            relaxed_working_dir_prefixes=["/workspace"],
        ),
    )

    assert input_doc["council"] == {
        "id": intent.council_id,
        "opa_policy_id": "git-workflow-local",
        "opa_relaxed_bare_command_words": ["gh", "git"],
        "opa_relaxed_working_dir_prefixes": ["/workspace"],
    }


def test_build_intent_filter_input_includes_shell_command_words():
    intent = Intent.create(
        agent_id="agent-opa",
        description="Run a piped shell command",
        target_resource="/workspace",
        command="/bin/echo hi | python3 scripts/run.py && /bin/cat output.txt",
        working_directory="/workspace",
        justification="Testing shell parsing",
    )

    input_doc = build_intent_filter_input(
        intent,
        request_type="authorize",
        authenticated=False,
    )

    assert input_doc["intent"]["shell_command_words"] == ["/bin/echo", "python3", "/bin/cat"]


def test_parse_opa_decision_accepts_boolean_result():
    decision = parse_opa_decision({"result": True, "decision_id": "opa-1"})
    assert decision == OPAPolicyDecision(allow=True, decision_id="opa-1")


def test_parse_opa_decision_accepts_structured_result():
    decision = parse_opa_decision(
        {
            "result": {
                "allow": False,
                "reasons": ["blocked"],
                "guidance": ["narrow scope"],
                "metadata": {"policy": "test"},
            },
            "decision_id": "opa-2",
        }
    )
    assert decision.allow is False
    assert decision.reasons == ["blocked"]
    assert decision.guidance == ["narrow scope"]
    assert decision.metadata == {"policy": "test"}
    assert decision.decision_id == "opa-2"


def test_parse_opa_decision_treats_undefined_as_deny():
    decision = parse_opa_decision({})
    assert decision.allow is False
    assert "undefined" in decision.reasons[0]


def test_parse_opa_decision_rejects_unusable_result():
    with pytest.raises(OPAEvaluationError, match="boolean 'allow'"):
        parse_opa_decision({"result": {"reasons": ["missing allow"]}})


@pytest.mark.asyncio
async def test_opa_http_client_posts_input_to_data_api():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            self.server.seen_path = self.path
            self.server.seen_payload = json.loads(self.rfile.read(length))
            response = {
                "decision_id": "decision-http",
                "result": {"allow": True, "metadata": {"source": "fake-opa"}},
            }
            body = json.dumps(response).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        client = OPAHttpClient(
            OPAConfig(
                enabled=True,
                url=f"http://{host}:{port}",
                decision_path="/v1/data/custom/intent/decision",
            )
        )
        try:
            decision = await client.evaluate({"intent": {"intent_id": "intent-http"}})
        finally:
            await client.aclose()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert decision.allow is True
    assert decision.decision_id == "decision-http"
    assert server.seen_path == "/v1/data/custom/intent/decision"
    assert server.seen_payload == {"input": {"intent": {"intent_id": "intent-http"}}}


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class _ClosesWithoutAnswering(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.close_connection = True

    def log_message(self, format, *args):
        return


class _TruncatesTheBody(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "4096")
        self.end_headers()
        self.wfile.write(b'{"result":{"allow":true')
        self.close_connection = True

    def log_message(self, format, *args):
        return


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", [_ClosesWithoutAnswering, _TruncatesTheBody])
async def test_opa_http_client_reports_a_failure_after_the_request_is_sent(handler):
    server, thread = _serve(handler)
    try:
        host, port = server.server_address
        client = OPAHttpClient(OPAConfig(enabled=True, url=f"http://{host}:{port}"))
        with pytest.raises(OPAEvaluationError, match="OPA request failed"):
            await client.evaluate({"intent": {"intent_id": "intent-http"}})
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


class _TruncatesAnErrorBody(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(503)
        self.send_header("Content-Length", "4096")
        self.end_headers()
        self.wfile.write(b"x")
        self.close_connection = True

    def log_message(self, format, *args):
        return


@pytest.mark.asyncio
async def test_opa_http_client_reports_an_http_error_whose_body_is_truncated():
    server, thread = _serve(_TruncatesAnErrorBody)
    try:
        host, port = server.server_address
        client = OPAHttpClient(OPAConfig(enabled=True, url=f"http://{host}:{port}"))
        with pytest.raises(OPAEvaluationError, match="OPA returned HTTP 503"):
            await client.evaluate({"intent": {"intent_id": "intent-http"}})
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ssl.SSLError(1, "[SSL: TLSV13_ALERT_CERTIFICATE_REQUIRED] certificate required"),
        ConnectionResetError(104, "Connection reset by peer"),
    ],
    ids=["tls-alert", "connection-reset"],
)
async def test_opa_http_client_reports_an_error_raised_while_reading_the_response(
    monkeypatch, error
):
    """The request is sent and the failure arrives with the read, so httpx does not wrap it."""
    client = OPAHttpClient(OPAConfig(enabled=True, url="http://127.0.0.1:1"))

    async def send(*args, **kwargs):
        raise error

    monkeypatch.setattr(client._http_client(), "send", send)
    try:
        with pytest.raises(OPAEvaluationError, match="OPA request failed") as raised:
            await client.evaluate({"intent": {"intent_id": "intent-http"}})
        assert raised.value.__cause__ is error
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "url",
    [
        "http://opa:secret@127.0.0.1:8181",  # pragma: allowlist secret
        "http://opa@127.0.0.1:8181",
    ],
)
def test_opa_http_client_refuses_credentials_in_a_plaintext_url(url):
    """The client would send them as Basic authentication with every request, in the clear."""
    with pytest.raises(ValueError, match="COUNCIL_OPA_URL") as refused:
        OPAHttpClient(OPAConfig(enabled=True, url=url))
    assert "secret" not in str(refused.value)


def test_opa_http_client_accepts_a_plaintext_url_without_credentials():
    OPAHttpClient(OPAConfig(enabled=True, url="http://127.0.0.1:8181"))
