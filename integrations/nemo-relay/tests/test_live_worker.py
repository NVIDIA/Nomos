# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Opt-in acceptance against operator-provided Nomos services (a real Council and Executor).

NOMOS_TEST_LIVE_CONFIG names a protected, operator-authored plugin configuration (its trust
snapshot, credentials as file references, and a fresh session). The test only issues two
/bin/echo commands and two destructive commands the Council must refuse. It provisions nothing.
"""

import json
import os
import uuid
from pathlib import Path

import pytest

if not (os.getenv("NOMOS_TEST_LIVE_CONFIG") and os.getenv("NOMOS_TEST_RELAY_BIN")):
    pytest.skip("opt-in existing Nomos council/executor test", allow_module_level=True)

from nemo_relay import plugin, tools
from support import INTENT, SIGNING_KEY, history
from test_installed_worker import ask_model, install, must_not_run

pytestmark = pytest.mark.installed


async def test_live_command_citation_denial_and_restart(tmp_path, monkeypatch):
    cfg = json.loads(Path(os.environ["NOMOS_TEST_LIVE_CONFIG"]).read_text())
    cfg["session"] = {"id": f"live-{uuid.uuid4().hex[:8]}"}
    cfg.setdefault("signing_key", SIGNING_KEY)  # the test cites evidence, which needs a signing key
    registry = install(tmp_path, cfg, monkeypatch)
    marker = uuid.uuid4().hex[:8]
    activation = await plugin.initialize(plugin.PluginConfig(), str(registry))
    try:
        seen = []
        turn = await ask_model(seen)
        assert seen[0]["tools"][0]["function"]["parameters"]["required"][-1] == "nomos"
        intent = {**INTENT, "turn": turn}
        first = await tools.execute(
            "run_shell",
            {"command": f"/bin/echo {marker}", "nomos": intent},
            must_not_run,
            tool_call_id="first",
        )
        assert first.result["status"] == "executed", json.dumps(first.result)
        assert (
            first.result["stdout"].strip() == marker and first.result["evidence_status"] == "locally_verified"
        )
        print("first:", json.dumps(first.annotation["nomos"]))
        second = await tools.execute(
            "run_shell",
            {
                "command": f"/bin/echo cited-{marker}",
                "nomos": {
                    "turn": turn,
                    "description": f"Print 'cited-{marker}' to record that the previous echo succeeded.",
                    "justification": "The earlier echo succeeded (see cited evidence); this confirms it in the log.",
                    "target_resource": "stdout of the workspace shell",
                    "blast_radius": "None: prints one line.",
                    "evidence": [first.result["evidence_id"]],
                },
            },
            must_not_run,
            tool_call_id="second",
        )
        assert second.result["status"] == "executed", second.result
        assert second.annotation["nomos"]["submitted"]["certified_citations"] == [first.result["evidence_id"]]
        destroy = {**intent, "description": "Delete everything.", "blast_radius": "The whole system."}
        # The pinned Nomos rejects a literal `rm -rf /` while validating the intent: an error, not a decision.
        invalid = await tools.execute(
            "run_shell", {"command": "/bin/rm -rf /", "nomos": destroy}, must_not_run, tool_call_id="invalid"
        )
        assert invalid.result["status"] == "council_error", invalid.result
        assert "dangerous pattern" in invalid.result["council"]["error"], invalid.result
        # The same intent past validation reaches the Council's gates and is denied.
        denied = await tools.execute(
            "run_shell",
            {"command": "/bin/rm -rf / --no-preserve-root", "nomos": destroy},
            must_not_run,
            tool_call_id="destroy",
        )
        print("denied:", json.dumps(denied.result))
        assert denied.result["status"] == "denied", denied.result
        assert denied.result["council"]["status"] == "denied"
    finally:
        await activation.close()
    # Evidence and turns live in the worker's memory: a new activation starts empty.
    activation = await plugin.initialize(plugin.PluginConfig(), str(registry))
    try:
        stale = await tools.execute(
            "run_shell",
            {
                "command": f"/bin/echo stale-{marker}",
                "nomos": {**INTENT, "turn": await ask_model([]), "evidence": [first.result["evidence_id"]]},
            },
            must_not_run,
            tool_call_id="stale",
        )
        assert stale.result["status"] == "evidence_not_found", stale.result
    finally:
        await activation.close()


async def test_live_without_a_signing_key_results_are_verified_but_never_stored(tmp_path, monkeypatch):
    cfg = json.loads(Path(os.environ["NOMOS_TEST_LIVE_CONFIG"]).read_text())
    cfg["session"] = {"id": f"live-{uuid.uuid4().hex[:8]}"}
    cfg.pop("signing_key", None)
    registry = install(tmp_path, cfg, monkeypatch)
    marker = uuid.uuid4().hex[:8]
    activation = await plugin.initialize(plugin.PluginConfig(), str(registry))
    try:
        seen = []
        turn = await ask_model(seen)
        nomos = seen[0]["tools"][0]["function"]["parameters"]["properties"]["nomos"]
        assert "evidence" not in nomos["properties"], "no signing key: nothing may be offered to cite"
        args = {"command": f"/bin/echo {marker}", "nomos": {**INTENT, "turn": turn}}
        out = await tools.execute("run_shell", args, must_not_run, tool_call_id="no-key")
        assert out.result["status"] == "executed", json.dumps(out.result)
        assert out.result["evidence_status"] == "locally_verified" and out.result["stdout"].strip() == marker
        assert "evidence_id" not in out.result
        ctx = type("Call", (), {"tool_name": "run_shell", "tool_call_id": "no-key", "args": args})()
        await ask_model(seen, history("check", (ctx, out.result)))
        assert "evidence catalog" not in seen[-1]["messages"][-1]["content"]
    finally:
        await activation.close()
