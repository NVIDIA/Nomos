# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Opt-in contract test against the reviewed Hermes Agent 0.21.3 source.

Run with the pinned source on ``PYTHONPATH``. The ordinary Nomos environment
does not install Hermes itself because the plugin is packaged into PA's Hermes
image, so this module skips when that host API is absent.
"""

from importlib.metadata import entry_points

import pytest

pytest.importorskip("hermes_cli.plugins")

from hermes_cli.plugins import PluginManager, PluginManifest

import nomos_hermes.plugin as nomos_plugin


def test_real_hermes_entrypoint_loader_registers_and_dispatches_valid_gate(monkeypatch):
    """Use Hermes's real loader; a function-target entry point fails this test."""

    class FakeGate:
        def __init__(self):
            self.pre_callback = self.pre_tool_call
            self.api_correlation = None

        def pre_tool_call(
            self,
            session_id="",
            task_id="",
            turn_id="",
            api_request_id="",
            **_,
        ):
            assert (session_id, task_id, turn_id, api_request_id) == self.api_correlation
            return {"action": "block", "message": "integration sentinel"}

        def post_tool_call(self, **_):
            return None

        def pre_api_request(
            self,
            session_id="",
            task_id="",
            turn_id="",
            api_request_id="",
            **_,
        ):
            self.api_correlation = (session_id, task_id, turn_id, api_request_id)
            return None

        def on_session_end(self, **_):
            return None

    monkeypatch.setattr(nomos_plugin, "_build_gate", FakeGate)
    entries = [ep for ep in entry_points(group="hermes_agent.plugins") if ep.name == "nomos"]
    assert len(entries) == 1
    assert entries[0].value == "nomos_hermes.plugin"

    manager = PluginManager()
    manifest = PluginManifest(
        name="nomos",
        source="entrypoint",
        path=entries[0].value,
        key="nomos",
    )
    manager._load_plugin(manifest)

    loaded = manager._plugins["nomos"]
    assert loaded.enabled is True
    assert loaded.error is None
    callbacks = manager.iter_hook_callbacks("pre_tool_call")
    assert len(callbacks) == 1
    assert len(manager.iter_hook_callbacks("post_tool_call")) == 1
    assert len(manager.iter_hook_callbacks("pre_api_request")) == 1
    assert len(manager.iter_hook_callbacks("on_session_end")) == 1
    correlation = {
        "session_id": "session-1",
        "task_id": "task-1",
        "turn_id": "turn-1",
        "api_request_id": "request-1",
    }
    assert manager.invoke_hook(
        "pre_api_request",
        request_messages=[{"role": "user", "content": "hello"}],
        user_message="hello",
        **correlation,
    ) == []
    assert manager.invoke_hook(
        "pre_tool_call", tool_name="terminal", args={}, **correlation
    ) == [
        {"action": "block", "message": "integration sentinel"}
    ]
