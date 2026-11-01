# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""End to end through Relay: every certified-evidence path, from the Executor's signature to the Council's check.

The real Relay CLI installs the bundle and the real host runs the worker; the application only calls
``llm.execute`` and ``tools.execute``. The loopback Nomos services use Nomos's own code wherever the answer
depends on it:

- the Executor verifies each writ with ``trusted_executor_contract.WritOfExecution.verify`` and signs each result
  with ``executors/common/evidence.py:certify_execution_result``, the function the real Executor uses;
- the Council parses every certified item with ``trusted_executor_contract.ExecutionResult``, verifies it with
  ``ExecutionEvidence.verify()``, and applies the rest of ``_validate_certified_evidence_result`` from
  ``nomos/nat/functions.py`` (registered key, fingerprint, council and agent ids, output hashes). A failure is an
  error response, as it is in Nomos.

Needs NOMOS_TEST_RELAY_BIN (as the installed-worker tests do) and the Nomos source (vendor/nomos, or
NOMOS_TEST_NOMOS_SOURCE).
"""

import asyncio
import base64
import contextlib
import hashlib
import importlib.util
import json
import os
import socket
import sys
from pathlib import Path

import pytest

if not os.getenv("NOMOS_TEST_RELAY_BIN"):
    pytest.skip("opt-in installed Relay acceptance test", allow_module_level=True)
NOMOS_SOURCE = Path(
    os.getenv("NOMOS_TEST_NOMOS_SOURCE")
    # the enclosing Nomos (integrations/nemo-relay), or the pinned submodule (standalone repository)
    or next(
        (
            source
            for parent in Path(__file__).resolve().parents
            for source in (parent, parent / "vendor" / "nomos")
            if (source / "trusted_executor_contract").is_dir()
        ),
        Path(__file__).resolve().parents[1] / "vendor" / "nomos",
    )
)
if not (NOMOS_SOURCE / "trusted_executor_contract").is_dir():
    pytest.skip(
        f"needs the Nomos source for its contract and signer ({NOMOS_SOURCE})", allow_module_level=True
    )
sys.path.append(str(NOMOS_SOURCE))

import uvicorn  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402
from nemo_relay import LLMRequest, llm, plugin  # noqa: E402
from nemo_relay.codecs import AnthropicMessagesCodec, OpenAIChatCodec, OpenAIResponsesCodec  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402
from support import (  # noqa: E402
    COUNCIL_KEY,
    EXECUTOR_KEY,
    SIGNING_KEY,
    TOOL,
    TURN,
    Council,
    base_config,
    call,
    cite,
    history,
    public,
)
from test_installed_worker import ask_model, install, run, write_config  # noqa: E402
from trusted_executor_contract.models import ExecutionResult, WritOfExecution  # noqa: E402

from nemo_relay_nomos.signing import PluginSigner  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "nomos_executor_evidence", NOMOS_SOURCE / "executors/common/evidence.py"
)
nomos_evidence = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = nomos_evidence
_spec.loader.exec_module(nomos_evidence)

pytestmark = pytest.mark.installed


def identity(key: Ed25519PrivateKey):
    return nomos_evidence.ExecutorIdentity(private_key=key, public_key_bytes=bytes.fromhex(public(key)))


def nomos_rejects(item: dict, *, agent_id: str, council_id: str, registry: dict[str, str]) -> str:
    """Nomos's certified-evidence check (nomos/nat/functions.py, ``_validate_certified_evidence_result`` and
    ``_validate_evidence_payload_matches_result``) on Nomos's own models. Empty means accepted."""
    result = ExecutionResult.model_validate(item)
    evidence = result.evidence
    if evidence is None:
        return "Certified evidence item is missing executor evidence"
    if evidence.algorithm.lower() != "ed25519":
        return f"Unsupported evidence signature algorithm: {evidence.algorithm}"
    public_key = bytes.fromhex(evidence.public_key_hex)
    if len(public_key) != 32:
        return "Certified evidence executor public key must be 32 bytes"
    if evidence.key_fingerprint != hashlib.sha256(public_key).hexdigest():
        return "Certified evidence key fingerprint does not match public key"
    if evidence.payload.executor_key_fingerprint != evidence.key_fingerprint:
        return "Certified evidence payload key fingerprint mismatch"
    if evidence.payload.council_id != council_id:
        return "Certified evidence council_id does not match request council_id"
    if evidence.payload.target_agent_id != agent_id:
        return "Certified evidence target_agent_id does not match request agent_id"
    if not evidence.verify():
        return "Certified evidence signature verification failed"
    registered = registry.get(evidence.key_fingerprint)
    if registered is None:
        return f"Certified evidence executor is not registered: {evidence.key_fingerprint}"
    if registered.lower() != evidence.public_key_hex.lower():
        return "Certified evidence public key does not match registered executor key"
    payload = evidence.payload
    if result.executor_id and payload.executor_id and result.executor_id != payload.executor_id:
        return "Certified evidence executor_id does not match result executor_id"
    if result.writ_id and payload.writ_id and result.writ_id != payload.writ_id:
        return "Certified evidence writ_id does not match result writ_id"
    if (
        hashlib.sha256(result.stdout.encode("utf-8", errors="replace")).hexdigest()
        != payload.result.stdout_sha256
    ):
        return "Certified evidence stdout hash does not match result stdout"
    if (
        hashlib.sha256(result.stderr.encode("utf-8", errors="replace")).hexdigest()
        != payload.result.stderr_sha256
    ):
        return "Certified evidence stderr hash does not match result stderr"
    for name in ("success", "exit_code", "error", "verification_stage"):
        if getattr(result, name) != getattr(payload.result, name):
            return f"Certified evidence {name} does not match result"
    return ""


class NomosServices:
    """A loopback Council (MCP) and Executor (HTTP) that verify and sign with Nomos's own code."""

    def __init__(self):
        self.council = Council()
        self.registry = {
            hashlib.sha256(bytes.fromhex(public(EXECUTOR_KEY))).hexdigest(): public(EXECUTOR_KEY)
        }
        self.checked: list[list[dict]] = []  # per Council call: each certified item and Nomos's verdict
        self.modes: list[str] = []  # the Executor's behaviour for its next calls (default "ok")
        self.results: list[dict] = []  # exactly what the Executor returned
        mcp = FastMCP("nomos-council", stateless_http=True, json_response=True)

        @mcp.tool()
        async def council_authorize(
            agent_id: str,
            auth_token: str,
            council_id: str,
            command: str,
            working_directory: str,
            description: str,
            justification: str,
            target_resource: str,
            blast_radius: str,
            context: str,
            evidence: list[str],
            certified_evidence: list[dict],
        ) -> dict:
            fields = {k: v for k, v in locals().items() if k != "self"}
            verdicts = [
                {
                    "item": item,
                    "error": nomos_rejects(
                        item, agent_id=agent_id, council_id=council_id, registry=self.registry
                    ),
                }
                for item in certified_evidence
            ]
            self.checked.append(verdicts)
            rejected = next((v["error"] for v in verdicts if v["error"]), "")
            if rejected:
                self.council.calls.append(fields)
                return {"status": "error", "error": rejected, "retryable": False}
            self.council.status = "denied" if command.startswith("/bin/rm") else "approved"
            return await self.council.authorize(**fields)

        async def execute(request):
            encoded = (await request.json())["writ"]
            writ = WritOfExecution.model_validate_json(base64.b64decode(encoded))
            assert writ.verify(bytes.fromhex(public(COUNCIL_KEY))), (
                "the Executor received a writ the Council did not sign"
            )
            assert request.headers["X-Agent-ID"] == writ.target_agent_id
            mode = self.modes.pop(0) if self.modes else "ok"
            if mode == "replay_previous":  # evidence bound to an earlier writ
                return JSONResponse(self.results[-1])
            stdout, stderr, success, exit_code, error, stage = "", "", True, 0, None, "passed"
            if mode == "refuse":
                success, exit_code, error, stage = False, None, "Tool not allowed: date", "tool_whitelist"
            elif mode == "fail":
                stderr, success, exit_code, error, stage = "boom\n", False, 1, "exit status 1", "execution"
            elif mode == "big":
                stdout = "x" * 4096 + "\n"
            else:
                stdout = writ.command.removeprefix("/bin/echo ") + "\n"
            result = ExecutionResult(
                executor_id="executor",
                writ_id=writ.writ_id,
                success=success,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                duration_seconds=0.01,
                error=error,
                verification_stage=stage,
            )
            key = Ed25519PrivateKey.generate() if mode == "foreign_key" else EXECUTOR_KEY
            signed = nomos_evidence.certify_execution_result(encoded, result, identity(key)).model_dump(
                mode="json"
            )
            if mode == "tamper":
                signed["stdout"] = "UNVERIFIED OUTPUT\n"
            self.results.append(signed)
            return JSONResponse(signed)

        self.app = mcp.streamable_http_app()
        self.app.routes.append(Route("/execute", execute, methods=["POST"]))

    async def __aenter__(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        self.server = uvicorn.Server(uvicorn.Config(self.app, log_level="error"))
        self.task = asyncio.create_task(self.server.serve(sockets=[sock]))
        while not self.server.started:
            if self.task.done():
                await self.task
            await asyncio.sleep(0.01)
        return self

    async def __aexit__(self, *exc):
        self.server.should_exit = True
        await self.task

    def certified_sent(self) -> list[dict]:
        """The certified items of the latest Council call."""
        return [v["item"] for v in self.checked[-1]]


class Conversation:
    """One application conversation: its own transcript goes with every model request."""

    def __init__(self, user: str = "check and cite"):
        self.messages = [{"role": "user", "content": user}]
        self.requests: list[dict] = []
        self.turn = None
        self.n = 0

    async def ask(self) -> str:
        """A model request through Relay; returns the Nomos message the provider received."""
        self.turn = await ask_model(self.requests, self.messages)
        return self.requests[-1]["messages"][-1]["content"]

    async def run(self, command: str, *cited: str, turn: str | None = None):
        self.n += 1
        token, call_id = turn or self.turn, f"{id(self)}-{self.n}"
        ctx = (
            cite(token, *cited, call_id=call_id, command=command)
            if cited
            else call(turn=token, call_id=call_id, command=command)
        )
        out = await run(ctx)
        self.messages += history("", (ctx, out.result))[1:]
        return out


@pytest.fixture(scope="module")
def installation(tmp_path_factory):
    """One installation of the bundle through the Relay CLI; each test writes its own config and activates it."""
    with pytest.MonkeyPatch.context() as env:
        yield install(tmp_path_factory.mktemp("relay"), base_config(), env)


@contextlib.asynccontextmanager
async def worker(registry: Path, *, drop: tuple[str, ...] = (), **overrides):
    """A fresh activation (a new worker, an empty store) on the real Relay host, wired to loopback Nomos."""
    cfg = base_config(**overrides)
    for key in drop:
        del cfg[key]
    async with NomosServices() as services:
        cfg["connection"].update(mcp_url=services.url + "/mcp", executor_url=services.url)
        write_config(registry, cfg)
        activation = await plugin.initialize(plugin.PluginConfig(), str(registry))
        try:
            yield services
        finally:
            await activation.close()


def stored(result: dict) -> str:
    return result["evidence_id"]


# ── 1–2: certified on arrival, and cited whole ───────────────────────────


async def test_an_executor_result_is_certified_on_arrival_and_listed_without_its_output(
    installation,
):
    async with worker(installation) as services:
        c = Conversation()
        await c.ask()
        out = await c.run("/bin/echo first-secret-output")
        assert out.result["status"] == "executed" and out.result["evidence_status"] == "locally_verified"
        assert out.result["stdout"] == "first-secret-output\n" and out.result["signer"]["type"] == "executor"
        assert (
            nomos_rejects(
                services.results[0], agent_id="agent", council_id="council", registry=services.registry
            )
            == ""
        )
        listed = await c.ask()
        line = next(ln for ln in listed.splitlines() if stored(out.result) in ln)
        assert "| run_shell | executed | certified |" in line and "first-secret-output\n" not in listed


async def test_a_cited_certified_result_reaches_the_council_whole_and_passes_nomos_validation(
    installation,
):
    async with worker(installation) as services:
        c = Conversation()
        await c.ask()
        first = await c.run("/bin/echo one")
        await c.ask()
        second = await c.run("/bin/echo two", stored(first.result))
        assert second.result["status"] == "executed", second.result
        assert services.certified_sent() == [services.results[0]], (
            "the Council must receive the Executor's exact result"
        )
        assert services.checked[-1][0]["error"] == "", services.checked[-1][0]["error"]
        assert services.council.calls[-1]["evidence"] == [], "a certified item must never go as asserted text"
        assert second.annotation["nomos"]["submitted"]["certified_citations"] == [stored(first.result)]


async def test_two_certified_citations_are_sent_in_order_and_both_validate(installation):
    async with worker(installation) as services:
        c = Conversation()
        await c.ask()
        a, b = await c.run("/bin/echo a"), await c.run("/bin/echo b")
        await c.ask()
        out = await c.run("/bin/echo both", stored(b.result), stored(a.result))
        assert out.result["status"] == "executed", out.result
        assert services.certified_sent() == [services.results[1], services.results[0]]
        assert [v["error"] for v in services.checked[-1]] == ["", ""]


async def test_a_cited_denial_is_certified_and_passes_nomos_validation(installation):
    async with worker(installation) as services:
        executor_id = register_plugin_key(services)
        c = Conversation()
        await c.ask()
        ok = await c.run("/bin/echo ok")
        denied = await c.run("/bin/rm -rf /tmp/x")
        assert denied.result["status"] == "denied" and denied.result["evidence_status"] == "plugin_certified"
        await c.ask()
        out = await c.run("/bin/echo after", stored(ok.result), stored(denied.result))
        assert out.result["status"] == "executed", out.result
        assert [v["error"] for v in services.checked[-1]] == ["", ""], "Nomos's own check accepts both"
        assert services.checked[-1][0]["item"] == services.results[0]
        decision = services.checked[-1][1]["item"]
        assert decision["executor_id"] == executor_id and decision["writ_id"] == ""
        assert json.loads(decision["stdout"])["status"] == "denied"
        assert services.council.calls[-1]["evidence"] == [], "nothing is sent as asserted evidence"


async def test_a_cited_denial_from_an_unregistered_key_closes_the_gate(installation):
    async with worker(installation) as services:
        c = Conversation()
        await c.ask()
        denied = await c.run("/bin/rm -rf /tmp/x")
        await c.ask()
        out = await c.run("/bin/echo after", stored(denied.result))
        assert out.result["status"] == "council_error" and "not registered" in out.result["message"], (
            out.result
        )
        assert not services.results, "nothing may run after the Council's refusal"


# ── 3: signed outcomes other than success ────────────────────────────────


@pytest.mark.parametrize(
    ("mode", "status", "execution", "outcome"),
    [
        ("refuse", "execution_failed", "not_executed", "not_executed"),
        ("fail", "execution_failed", "failed", "failed"),
    ],
)
async def test_a_signed_refusal_or_failed_run_is_certified_and_citable(
    installation, mode, status, execution, outcome
):
    async with worker(installation) as services:
        services.modes = [mode]
        c = Conversation()
        await c.ask()
        out = await c.run("/bin/date")
        assert (out.result["status"], out.result["execution_status"]) == (status, execution), out.result
        assert out.result["evidence_status"] == "locally_verified"
        listed = await c.ask()
        assert f"{stored(out.result)} | run_shell | {outcome} | certified |" in listed
        cited = await c.run("/bin/echo noted", stored(out.result))
        assert cited.result["status"] == "executed"
        assert services.certified_sent() == [services.results[0]] and services.checked[-1][0]["error"] == ""


# ── 4: results that fail verification ────────────────────────────────────


@pytest.mark.parametrize("mode", ["tamper", "foreign_key", "replay_previous"])
async def test_an_unverifiable_result_is_withheld_never_listed_and_blocks_only_its_conversation(
    installation, mode
):
    async with worker(installation) as services:
        c = Conversation()
        await c.ask()
        if mode == "replay_previous":
            await c.run("/bin/echo earlier")
        services.modes = [mode]
        bad = await c.run("/bin/echo hidden")
        assert bad.result["status"] == "evidence_invalid" and bad.result["execution_status"] == "unknown", (
            bad.result
        )
        assert "stdout" not in bad.result and "UNVERIFIED OUTPUT" not in json.dumps(bad.result)
        listed = await c.ask()
        assert stored(bad.result) not in listed, "a quarantined result must never be offered"
        blocked = await c.run("/bin/echo next")
        assert blocked.result["status"] == "session_requires_reconciliation"
        other = Conversation("another user")
        await other.ask()
        assert (await other.run("/bin/echo fine")).result["status"] == "executed"
        assert all(v["error"] == "" for calls in services.checked for v in calls), (
            "the Council saw a bad item"
        )


# ── 5: limits and boundaries ─────────────────────────────────────────────


async def test_a_certified_citation_over_the_size_limit_is_refused_before_the_council(installation):
    async with worker(installation, limits={"max_citation_bytes": 2048}) as services:
        services.modes = ["big"]
        c = Conversation()
        await c.ask()
        big = await c.run("/bin/echo big")
        assert big.result["status"] == "executed"
        calls = len(services.council.calls)
        await c.ask()
        out = await c.run("/bin/echo cite-big", stored(big.result))
        assert out.result["status"] == "citation_size_exceeded" and len(services.council.calls) == calls


async def test_another_conversation_cannot_cite_a_certified_result(installation):
    async with worker(installation) as services:
        alice, bob = Conversation("Alice"), Conversation("Bob")
        await alice.ask()
        await bob.ask()
        secret = await alice.run("/bin/echo alice-only")
        calls = len(services.council.calls)
        out = await bob.run("/bin/echo borrow", stored(secret.result))
        assert out.result["status"] == "evidence_not_found" and len(services.council.calls) == calls


async def test_a_restarted_worker_holds_no_certified_evidence(installation):
    async with worker(installation) as services:
        c = Conversation()
        await c.ask()
        first = await c.run("/bin/echo before-restart")
        old_turn = c.turn
    # A new activation of the same installation: a new worker, an empty store.
    async with worker(installation) as services:
        stale = await c.run("/bin/echo after", stored(first.result), turn=old_turn)
        assert stale.result["status"] == "unknown_turn"
        await c.ask()
        assert stored(first.result) not in c.requests[-1]["messages"][-1]["content"]
        gone = await c.run("/bin/echo after", stored(first.result))
        assert gone.result["status"] == "evidence_not_found" and services.council.calls == []


async def test_replaying_a_certified_call_returns_the_stored_result_without_executing_again(
    installation,
):
    async with worker(installation) as services:
        c = Conversation()
        await c.ask()
        ctx = call(turn=c.turn, call_id="once", command="/bin/echo once")
        first, again = await run(ctx), await run(ctx)
        assert (
            again.result == first.result and len(services.results) == 1 and len(services.council.calls) == 1
        )


async def test_a_council_that_does_not_trust_the_executor_key_is_a_closed_gate(installation):
    """The operator pinned a key in the plugin that the Council's own registry does not hold."""
    async with worker(installation) as services:
        c = Conversation()
        await c.ask()
        first = await c.run("/bin/echo pinned-but-unregistered")
        services.registry.clear()
        await c.ask()
        out = await c.run("/bin/echo cite-it", stored(first.result))
        assert out.result["status"] == "council_error" and "not registered" in out.result["message"], (
            out.result
        )
        assert len(services.results) == 1, "nothing may run after the Council's refusal"
        listed = await c.ask()
        assert stored(first.result) in listed, "the record itself is still certified and listed"


# ── 6: no signing key ────────────────────────────────────────────────────


async def test_without_a_signing_key_results_are_verified_but_never_stored_or_offered(installation):
    async with worker(installation, drop=("signing_key",)) as services:
        c = Conversation()
        await c.ask()
        ok = await c.run("/bin/echo verified-anyway")
        assert ok.result["status"] == "executed" and ok.result["evidence_status"] == "locally_verified"
        assert "evidence_id" not in ok.result and ok.result["stdout"] == "verified-anyway\n"
        services.modes = ["tamper"]
        bad = await c.run("/bin/echo hidden")
        assert bad.result["status"] == "evidence_invalid" and "stdout" not in bad.result
        listed = await c.ask()
        assert "evidence catalog" not in listed
        nomos = c.requests[-1]["tools"][0]["function"]["parameters"]["properties"]["nomos"]
        assert "evidence" not in nomos["properties"]


# ── 7: observed tool definitions, certified ──────────────────────────────


def register_plugin_key(services) -> str:
    """The operator's step: register the plugin's signing key as a trusted executor; returns its executor id."""
    signer = PluginSigner(bytes.fromhex(SIGNING_KEY))
    services.registry[signer.key_fingerprint] = signer.public_key_hex
    return signer.executor_id


async def test_tool_definitions_reach_the_council_certified_and_pass_nomos_validation(installation):
    async with worker(installation, tool_definitions="called") as services:
        executor_id = register_plugin_key(services)
        c = Conversation()
        await c.ask()
        out = await c.run("/bin/echo with-definitions")
        assert out.result["status"] == "executed", out.result
        [verdict] = services.checked[-1]
        assert verdict["error"] == "", "Nomos's own certified-evidence check accepts it"
        item = verdict["item"]
        assert item["executor_id"] == executor_id and item["writ_id"] == ""
        observed = json.loads(item["stdout"])
        assert observed["called"] == TOOL and observed["tool_count"] == 2, "as the application offered it"
        sent = services.council.calls[-1]
        assert sent["evidence"] == [], "nothing asserted, so allow_uncertified_evidence = false accepts it"
        digest = item["evidence"]["payload"]["result"]["stdout_sha256"]
        assert f"tool definitions stdout_sha256={digest}" in sent["context"]


async def test_tool_definitions_from_an_unregistered_key_close_the_gate(installation):
    async with worker(installation, tool_definitions="called") as services:
        c = Conversation()
        await c.ask()
        out = await c.run("/bin/echo not-registered")
        assert out.result["status"] == "council_error" and "not registered" in out.result["message"], (
            out.result
        )
        assert not services.results, "nothing may run after the Council's refusal"


# ── 8: the catalog through each Relay codec ──────────────────────────────

SHELL_SCHEMA = {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}


def _openai_chat(ctx, result):
    body = {
        "model": "m",
        "messages": history("check", (ctx, result)),
        "tools": [TOOL],
    }
    return body, OpenAIChatCodec()


def _openai_responses(ctx, result):
    body = {
        "model": "m",
        "input": [
            {"role": "user", "content": "check"},
            {
                "type": "function_call",
                "call_id": ctx.tool_call_id,
                "name": "run_shell",
                "arguments": json.dumps(ctx.args),
            },
            {"type": "function_call_output", "call_id": ctx.tool_call_id, "output": json.dumps(result)},
        ],
        "tools": [{"type": "function", "name": "run_shell", "parameters": SHELL_SCHEMA}],
    }
    return body, OpenAIResponsesCodec()


def _anthropic(ctx, result):
    body = {
        "model": "m",
        "max_tokens": 64,
        "system": "be brief",
        "messages": [
            {"role": "user", "content": "check"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": ctx.tool_call_id, "name": "run_shell", "input": ctx.args}
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": ctx.tool_call_id, "content": json.dumps(result)}
                ],
            },
        ],
        "tools": [{"name": "run_shell", "description": "run a shell command", "input_schema": SHELL_SCHEMA}],
    }
    return body, AnthropicMessagesCodec()


def _nomos_message(api: str, content: dict) -> str:
    if api == "anthropic":
        system = content["system"]
        return system if isinstance(system, str) else "\n".join(part.get("text", "") for part in system)
    if api == "responses":
        last = content["input"][-1]
        assert last.get("role") == "developer", last
        text = last["content"]
        return text if isinstance(text, str) else "\n".join(part.get("text", "") for part in text)
    last = content["messages"][-1]
    assert last["role"] == "system", last
    return last["content"]


def _nomos_schema(api: str, content: dict) -> dict:
    tool = content["tools"][0]
    if api == "anthropic":
        return tool["input_schema"]["properties"]["nomos"]
    if api == "responses":
        return tool["parameters"]["properties"]["nomos"]
    return tool["function"]["parameters"]["properties"]["nomos"]


@pytest.mark.parametrize(
    ("api", "build"), [("chat", _openai_chat), ("responses", _openai_responses), ("anthropic", _anthropic)]
)
async def test_the_certified_catalog_reaches_the_model_through_each_relay_codec(installation, api, build):
    async with worker(installation):
        c = Conversation()
        await c.ask()
        ctx = call(turn=c.turn, call_id="call_codec", command="/bin/echo through-a-codec")
        result = (await run(ctx)).result
        body, codec = build(ctx, result)
        seen = []

        async def provider(request):
            seen.append(request.content)
            return {}

        await llm.execute("model", LLMRequest({}, body), provider, codec=codec)
        message = _nomos_message(api, seen[0])
        assert TURN.search(message), f"{api}: no turn token in {message!r}"
        assert f"{stored(result)} | run_shell | executed | certified |" in message, f"{api}: {message!r}"
        assert "through-a-codec\n" not in message
        assert "evidence" in _nomos_schema(api, seen[0])["properties"], (
            f"{api}: the evidence field was not injected"
        )
