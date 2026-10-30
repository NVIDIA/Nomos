# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Opt-in acceptance test: the real Relay CLI installs the bundle, the real host runs the worker.

An application with no Nomos code makes ordinary ``llm.execute`` / ``tools.execute``
calls. Loopback MCP (Council) and HTTP (Executor) services sign with test-only keys.

Set NOMOS_TEST_RELAY_BIN to the Relay CLI matching the installed ``nemo-relay``
binding (the test group installs both at 0.10.0). ``plugins add`` pip-installs the
bundle into a fresh environment; set NOMOS_TEST_WHEELHOUSE to a directory of wheels
(``scripts/wheelhouse.sh``) to install offline.
"""

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

if not os.getenv("NOMOS_TEST_RELAY_BIN"):
    pytest.skip("opt-in installed Relay acceptance test", allow_module_level=True)

import tomli_w
import uvicorn
from mcp.server.fastmcp import FastMCP
from nemo_relay import LLMRequest, ToolExecutionResult, llm, plugin, tools
from nemo_relay.codecs import OpenAIChatCodec
from starlette.responses import JSONResponse
from starlette.routing import Route
from support import INTENT, LOCAL_TOOL, TOOL, TURN, Council, base_config, call, cite, history, sign_result

pytestmark = pytest.mark.installed
PACKAGE = Path(__file__).resolve().parents[1]
BUNDLE_FILES = ("pyproject.toml", "relay-plugin.toml", "LICENSE", "NOTICE", "README.md")


def build_bundle(directory: Path) -> Path:
    bundle = directory / "bundle"
    bundle.mkdir()
    shutil.copytree(
        PACKAGE / "nemo_relay_nomos",
        bundle / "nemo_relay_nomos",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    for name in BUNDLE_FILES:
        shutil.copy2(PACKAGE / name, bundle / name)
    manifest = bundle / "relay-plugin.toml"
    data = tomllib.loads(manifest.read_text())
    artifact = (bundle / data["source"]["artifact"]).read_bytes()
    assert data["integrity"]["sha256"] == "sha256:" + hashlib.sha256(artifact).hexdigest(), "stale integrity"
    return manifest


def relay(directory: Path, *args: str, timeout: int = 600) -> subprocess.CompletedProcess:
    cli = [os.environ["NOMOS_TEST_RELAY_BIN"], "--config", str(directory / "config.toml")]
    cli += ["--plugin-config-path", str(directory / "plugins.toml"), *args]
    return subprocess.run(cli, capture_output=True, text=True, timeout=timeout)


def install(directory: Path, cfg: dict, monkeypatch) -> Path:
    manifest = build_bundle(directory)
    (directory / "config.toml").write_text("")
    registry = directory / "plugins.toml"
    policy = {"plugins": {"policy": {"defaults": {"attestation": "integrity_only", "startup": "required"}}}}
    registry.write_text(tomli_w.dumps(policy))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(directory / "xdg"))
    monkeypatch.setenv("NEMO_RELAY_TEST_SKIP_IMPLICIT_CONFIG", "1")
    monkeypatch.setenv("NEMO_RELAY_PYTHON", sys.executable)
    if os.getenv("NOMOS_TEST_WHEELHOUSE"):
        monkeypatch.setenv("PIP_NO_INDEX", "1")
        monkeypatch.setenv("PIP_FIND_LINKS", os.environ["NOMOS_TEST_WHEELHOUSE"])
    added = relay(directory, "plugins", "add", str(manifest))
    assert added.returncode == 0, added.stderr[-4000:] + added.stdout[-2000:]
    write_config(registry, cfg)
    enabled = relay(directory, "plugins", "enable", "nomos.council", timeout=60)
    assert enabled.returncode == 0, enabled.stderr
    return registry


def write_config(registry: Path, cfg: dict) -> None:
    document = tomllib.loads(registry.read_text())
    document["plugins"]["dynamic"][0]["config"] = cfg
    registry.write_text(tomli_w.dumps(document))


class Services:
    """A loopback Council (MCP) and Executor (HTTP) with test-only signing keys."""

    def __init__(self):
        self.council, self.results = Council(), []
        self.tamper_next = False
        self.council_delay_s = 0.0
        mcp = FastMCP("test-council", stateless_http=True, json_response=True)

        @mcp.tool()
        async def council_authorize(
            agent_id: str,
            auth_token: str,
            council_id: str,
            description: str,
            justification: str,
            target_resource: str,
            blast_radius: str,
            evidence: list[str],
            certified_evidence: list[dict],
            command: str = "",
            working_directory: str = "",
            context: str = "",
            api_version: str = "v1",
            tool_call: dict | None = None,
        ) -> dict:
            fields = {k: v for k, v in locals().items() if k != "self"}
            await asyncio.sleep(self.council_delay_s)
            self.council.status = "denied" if command.startswith("/bin/rm") else "approved"
            return await self.council.authorize(**fields)

        @mcp.tool()
        async def council_get_public_key(council_id: str = "", api_version: str = "v1") -> dict:
            return await self.council.public_key(council_id)

        async def execute(request):
            body = await request.json()
            writ = json.loads(base64.b64decode(body["writ"]))
            assert request.headers["X-Agent-ID"] == writ["target_agent_id"]
            result = sign_result(writ)
            if self.tamper_next:
                result["stdout"], self.tamper_next = "UNVERIFIED OUTPUT", False
            self.results.append(result)
            return JSONResponse(result)

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


def must_not_run(args):
    raise AssertionError(f"the application's own run_shell ran with {args}")


async def ask_model(provider_requests, messages=None):
    """One model request through Relay; returns the turn token the provider saw in the Nomos message."""

    async def model_provider(request):
        provider_requests.append(request.content)
        return {"tool_calls": []}

    messages = messages or [{"role": "user", "content": "check and cite"}]
    await llm.execute(
        "model", LLMRequest({}, {"messages": messages, "tools": [TOOL, LOCAL_TOOL]}), model_provider
    )
    return TURN.search(provider_requests[-1]["messages"][-1]["content"])[0]


async def run(ctx, func=must_not_run):
    return await tools.execute(ctx.tool_name, ctx.args, func, tool_call_id=ctx.tool_call_id)


async def test_installed_worker_governs_an_uninstrumented_application(tmp_path, monkeypatch):
    cfg = base_config(
        guard={"include": ["run_shell", "read_file"]},
        tools={
            "run_shell": {"executor": "nomos_http", "command_argument": "command"},
            "read_file": {"executor": "local"},
        },
        tool_call={"subject": "opaque-test-user"},
    )
    token = tmp_path / "token"
    token.write_text("worker-file-token\n")
    token.chmod(0o600)
    cfg["connection"]["auth_token"] = {"file": str(token)}
    async with Services() as services:
        cfg["connection"].update(mcp_url=services.url + "/mcp", executor_url=services.url)
        registry = install(tmp_path, cfg, monkeypatch)
        activation = await plugin.initialize(plugin.PluginConfig(), str(registry))
        try:
            # H1: the model sees the required intent on guarded tools only, and this request's turn token.
            requests = []
            t = await ask_model(requests)
            params = [tool["function"]["parameters"] for tool in requests[0]["tools"]]
            assert all(p["required"][-1] == "nomos" for p in params)

            # H3, nomos_http: the Executor runs it; the application's function never does.
            first_call = call(turn=t, call_id="first", command="echo hello")
            first = await run(first_call)
            assert first.result["status"] == "executed", first.result
            assert first.result["evidence_status"] == "locally_verified"
            assert services.council.calls[0]["auth_token"] == "worker-file-token"
            assert first.annotation["nomos"]["evidence_id"] == first.result["evidence_id"]
            eid = first.result["evidence_id"]

            # H3, local: the application's function runs after approval, without `nomos`.
            seen = []
            local_call = call(name="read_file", turn=t, call_id="local", path="/etc/hostname")
            local = await run(
                local_call, lambda args: (seen.append(args), ToolExecutionResult({"text": "host"}))[1]
            )
            assert local.result["output"] == {"text": "host"} and seen == [{"path": "/etc/hostname"}]
            structured_request = services.council.calls[1]
            assert structured_request["tool_call"] == {
                "schema_version": 1,
                "tool_name": "read_file",
                "arguments": {"path": "/etc/hostname"},
                "subject": "opaque-test-user",
            }

            # Denial: the full Council response comes back from tools.execute.
            denied_call = call(turn=t, call_id="deny", command="/bin/rm -rf /")
            denied = await run(denied_call)
            assert denied.result["status"] == "denied"
            assert denied.result["council"]["council_members"][0]["member_id"] == "blast-containment"

            # The next model request, carrying the transcript, continues the conversation and lists its
            # evidence: tool and arguments, never the output.
            seen_so_far = history(
                "check and cite",
                (first_call, first.result),
                (local_call, local.result),
                (denied_call, denied.result),
            )
            t2 = await ask_model(requests, seen_so_far)
            catalog = requests[-1]["messages"][-1]["content"]
            assert eid in catalog and '"command":"echo hello"' in catalog and "second" not in catalog
            assert denied.result["evidence_id"] in catalog and local.result["evidence_id"] in catalog

            # Citation: the cited record reaches the Council as the full signed original.
            second = await run(cite(t2, eid, call_id="second", command="echo next"))
            assert second.result["status"] == "executed"
            assert services.council.calls[-1]["certified_evidence"] == [services.results[0]]

            # Replay: the same call id and arguments return the stored outcome; nothing is re-sent.
            replay = await run(first_call)
            assert replay.result["evidence_id"] == eid and len(services.results) == 2

            # Unguarded tools are untouched.
            plain = await tools.execute("unguarded", {}, lambda args: ToolExecutionResult({"local": True}))
            assert plain.result == {"local": True}

            # A tampered Executor result is quarantined, and its output withheld.
            services.tamper_next = True
            bad = await run(call(turn=t2, call_id="bad", command="echo tamper"))
            assert bad.result["evidence_status"] == "invalid" and "stdout" not in bad.result
        finally:
            await activation.close()

        # Restart: evidence and turns live in the worker's memory, so a new activation starts empty.
        council_calls = len(services.council.calls)
        activation = await plugin.initialize(plugin.PluginConfig(), str(registry))
        try:
            old_turn = await run(cite(t2, eid, call_id="stale", command="echo stale"))
            assert old_turn.result["status"] == "unknown_turn", old_turn.result
            t3 = await ask_model(requests, seen_so_far)
            assert eid not in requests[-1]["messages"][-1]["content"], "a new worker lists no old evidence"
            stale = await run(cite(t3, eid, call_id="stale", command="echo stale"))
            assert stale.result["status"] == "evidence_not_found", stale.result
            assert len(services.council.calls) == council_calls, "a stale citation must not reach the Council"
        finally:
            await activation.close()


async def test_without_a_signing_key_the_installed_worker_offers_and_stores_no_evidence(
    tmp_path, monkeypatch
):
    cfg = base_config()
    del cfg["signing_key"]
    async with Services() as services:
        cfg["connection"].update(mcp_url=services.url + "/mcp", executor_url=services.url)
        registry = install(tmp_path, cfg, monkeypatch)
        activation = await plugin.initialize(plugin.PluginConfig(), str(registry))
        try:
            requests = []
            t = await ask_model(requests)
            nomos = requests[0]["tools"][0]["function"]["parameters"]["properties"]["nomos"]
            assert "evidence" not in nomos["properties"]
            first_call = call(turn=t, call_id="first", command="echo hello")
            first = await run(first_call)
            assert first.result["status"] == "executed" and "evidence_id" not in first.result, first.result
            await ask_model(requests, history("check and cite", (first_call, first.result)))
            assert requests[-1]["messages"][-1]["content"].startswith("Nomos turn: nt_")
            assert "evidence catalog" not in requests[-1]["messages"][-1]["content"]
        finally:
            await activation.close()


async def test_worker_environment_is_stripped_so_env_references_fail_closed(tmp_path, monkeypatch):
    """Pins the Relay finding: a managed worker does not inherit the host's environment."""
    monkeypatch.setenv("NOMOS_TEST_TOKEN", "present-in-host")
    cfg = base_config()
    cfg["connection"]["auth_token"] = {"env": "NOMOS_TEST_TOKEN"}
    registry = install(tmp_path, cfg, monkeypatch)
    with pytest.raises(Exception, match="NOMOS_TEST_TOKEN"):
        activation = await plugin.initialize(plugin.PluginConfig(), str(registry))
        await activation.close()


async def test_the_installed_worker_can_take_the_council_key_from_the_council(tmp_path, monkeypatch):
    """trust.council_public_key_source = "council": the key comes over MCP on the first guarded call."""
    cfg = base_config()
    del cfg["trust"]["council_public_key_hex"]
    cfg["trust"]["council_public_key_source"] = "council"
    async with Services() as services:
        cfg["connection"].update(mcp_url=services.url + "/mcp", executor_url=services.url)
        registry = install(tmp_path, cfg, monkeypatch)
        activation = await plugin.initialize(plugin.PluginConfig(), str(registry))
        try:
            requests = []
            t = await ask_model(requests)
            assert services.council.key_calls == [], "nothing is fetched before a guarded call"
            first = await run(call(turn=t, call_id="first", command="echo hello"))
            second = await run(call(turn=t, call_id="second", command="echo again"))
            assert first.result["status"] == second.result["status"] == "executed", (
                first.result,
                second.result,
            )
            assert services.council.key_calls == ["council"]
        finally:
            await activation.close()


async def test_relay_codec_rejects_two_edited_tools_in_one_request(tmp_path, monkeypatch):
    """Pins Relay finding R13: a request codec cannot patch back more than one edited tool."""
    registry = install(tmp_path, base_config(guard={"include": ["run_shell", "read_file"]}), monkeypatch)
    activation = await plugin.initialize(plugin.PluginConfig(), str(registry))
    try:
        body = {"model": "m", "messages": [{"role": "user", "content": "check"}], "tools": [TOOL, LOCAL_TOOL]}

        async def model_provider(request):
            raise AssertionError("the request must fail at encode, before the provider")

        with pytest.raises(RuntimeError, match="multiple edited array items without stable identities"):
            await llm.execute("model", LLMRequest({}, body), model_provider, codec=OpenAIChatCodec())
    finally:
        await activation.close()


@pytest.mark.skipif(not os.getenv("NOMOS_TEST_SLOW"), reason="opt-in: takes about 40 s (NOMOS_TEST_SLOW=1)")
async def test_a_slow_council_is_not_cut_off_by_a_worker_rpc_deadline(tmp_path, monkeypatch):
    """Pins the Relay 0.10.0 finding: tool execution intercepts have no 30 s worker RPC deadline (#1182)."""
    cfg = base_config()
    async with Services() as services:
        services.council_delay_s = 35.0  # longer than WORKER_RPC_TIMEOUT
        cfg["connection"].update(mcp_url=services.url + "/mcp", executor_url=services.url)
        registry = install(tmp_path, cfg, monkeypatch)
        activation = await plugin.initialize(plugin.PluginConfig(), str(registry))
        try:
            slow = await run(call(turn=await ask_model([]), call_id="slow", command="echo slow"))
        finally:
            await activation.close()
    assert slow.result["status"] == "executed", slow.result
    assert slow.annotation["nomos"]["timings_ms"]["council"] >= 35000


def live_worker_pids() -> list[int]:
    """This process's live child workers running the plugin (Linux /proc; zombies excluded)."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = dict(
                line.split(":", 1) for line in (entry / "status").read_text().splitlines() if ":" in line
            )
            cmdline = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        alive = not status["State"].strip().startswith("Z")
        if alive and int(status["PPid"]) == os.getpid() and b"nemo_relay_nomos" in cmdline:
            found.append(int(entry.name))
    return found


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="needs /proc")
async def test_a_dead_worker_is_not_respawned_and_guarded_calls_fail_closed(tmp_path, monkeypatch):
    """Pins the Relay 0.10.0 finding: one long-lived worker per activation, never respawned; a dead worker
    fails closed. The in-memory evidence store relies on both."""
    cfg = base_config()
    async with Services() as services:
        cfg["connection"].update(mcp_url=services.url + "/mcp", executor_url=services.url)
        registry = install(tmp_path, cfg, monkeypatch)
        activation = await plugin.initialize(plugin.PluginConfig(), str(registry))
        ran = []
        try:
            t = await ask_model([])
            first = await run(call(turn=t, call_id="first", command="echo first"))
            assert first.result["status"] == "executed", first.result
            pids = live_worker_pids()
            assert len(pids) == 1, f"expected one worker for the activation, found {pids}"
            os.kill(pids[0], signal.SIGKILL)
            for _ in range(100):
                if not live_worker_pids():
                    break
                await asyncio.sleep(0.05)
            with pytest.raises(Exception) as failure:  # noqa: B017 - Relay reports the intercept failure as an error
                await asyncio.wait_for(
                    tools.execute(
                        "run_shell",
                        {"command": "echo after", "nomos": {**INTENT, "turn": t}},
                        lambda args: ran.append(args),
                        tool_call_id="after",
                    ),
                    timeout=30,
                )
            print("after the worker died:", type(failure.value).__name__, str(failure.value)[:300])
            assert ran == [], "the tool ran although its governing worker was dead"
            assert live_worker_pids() == [], "Relay respawned the worker"
            assert len(services.council.calls) == 1, "a call reached the Council after the worker died"
        finally:
            with contextlib.suppress(Exception):  # tearing down an activation whose worker is already dead
                await activation.close()
