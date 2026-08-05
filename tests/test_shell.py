# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the shell command execution handler."""

import pytest

from nomos.level_3.executor.shell import (
    ShellConfig,
    ShellResult,
    _build_env,
    execute_shell_command,
)


class TestShellResult:
    def test_succeeded_true(self):
        r = ShellResult(exit_code=0, stdout="ok", stderr="")
        assert r.succeeded is True

    def test_succeeded_false_nonzero(self):
        r = ShellResult(exit_code=1, stdout="", stderr="err")
        assert r.succeeded is False

    def test_succeeded_false_timeout(self):
        r = ShellResult(exit_code=-1, stdout="", stderr="", timed_out=True)
        assert r.succeeded is False

    def test_to_dict(self):
        r = ShellResult(exit_code=0, stdout="hi", stderr="", command="echo hi")
        d = r.to_dict()
        assert d["exit_code"] == 0
        assert d["stdout"] == "hi"
        assert d["command"] == "echo hi"
        assert d["succeeded"] is True


class TestBuildEnv:
    def test_copies_allowed_vars(self):
        cfg = ShellConfig(allowed_env_vars=["PATH", "HOME"])
        env = _build_env(cfg)
        assert "PATH" in env or "HOME" in env

    def test_blocks_secret_vars(self):
        cfg = ShellConfig(blocked_env_vars=["SECRET_KEY"])
        env = _build_env(cfg, extra_vars={"SECRET_KEY": "leaked", "SAFE": "ok"})
        assert "SECRET_KEY" not in env
        assert env.get("SAFE") == "ok"


class TestExecuteShellCommand:
    @pytest.mark.asyncio
    async def test_simple_echo(self):
        result = await execute_shell_command("echo hello")
        assert result.succeeded
        assert result.exit_code == 0
        assert "hello" in result.stdout

    @pytest.mark.asyncio
    async def test_exit_code(self):
        result = await execute_shell_command("exit 42")
        assert not result.succeeded
        assert result.exit_code == 42

    @pytest.mark.asyncio
    async def test_stderr(self):
        result = await execute_shell_command("echo oops >&2")
        assert result.exit_code == 0
        assert "oops" in result.stderr

    @pytest.mark.asyncio
    async def test_working_directory(self, tmp_path):
        result = await execute_shell_command("pwd", working_directory=str(tmp_path))
        assert result.succeeded
        assert str(tmp_path) in result.stdout

    @pytest.mark.asyncio
    async def test_invalid_working_directory(self):
        result = await execute_shell_command("echo hi", working_directory="/nonexistent/path/xyz")
        assert not result.succeeded
        assert "does not exist" in result.stderr

    @pytest.mark.asyncio
    async def test_timeout(self):
        result = await execute_shell_command("sleep 10", timeout_seconds=1)
        assert not result.succeeded
        assert result.timed_out
        assert result.duration_seconds >= 0.9

    @pytest.mark.asyncio
    async def test_duration_tracked(self):
        result = await execute_shell_command("sleep 0.1")
        assert result.succeeded
        assert result.duration_seconds >= 0.05

    @pytest.mark.asyncio
    async def test_multiline_output(self):
        result = await execute_shell_command("echo line1 && echo line2")
        assert result.succeeded
        assert "line1" in result.stdout
        assert "line2" in result.stdout
