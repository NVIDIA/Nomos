# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for executor config Pydantic validation (config_schema.py).
"""

import pytest
from pydantic import ValidationError

from executors.docker.config import ExecutorConfig
from executors.docker.config_schema import (
    ExecutorSection,
    ExecutorYamlConfig,
    FilesystemSection,
    RedisSection,
    SecuritySection,
    ToolsSection,
)


class TestExecutorSection:
    def test_defaults(self):
        s = ExecutorSection()
        assert s.listen_host == "127.0.0.1"
        assert s.listen_port == 9902
        assert s.log_level == "INFO"
        assert s.max_command_timeout == 60
        assert s.identity_private_key_path == ""
        assert s.identity_public_key_path == ""

    def test_valid_log_levels(self):
        for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            s = ExecutorSection(log_level=level)
            assert s.log_level == level

    def test_invalid_log_level(self):
        with pytest.raises(ValidationError, match="log_level"):
            ExecutorSection(log_level="VERBOSE")

    def test_port_too_high(self):
        with pytest.raises(ValidationError, match="listen_port"):
            ExecutorSection(listen_port=99999)

    def test_port_too_low(self):
        with pytest.raises(ValidationError, match="listen_port"):
            ExecutorSection(listen_port=0)

    def test_timeout_too_high(self):
        with pytest.raises(ValidationError, match="max_command_timeout"):
            ExecutorSection(max_command_timeout=9999)

    def test_timeout_zero(self):
        with pytest.raises(ValidationError, match="max_command_timeout"):
            ExecutorSection(max_command_timeout=0)

    def test_passthrough_env_safe_vars(self):
        s = ExecutorSection(passthrough_env=["HOME", "KUBECONFIG", "PATH"])
        assert s.passthrough_env == ["HOME", "KUBECONFIG", "PATH"]

    def test_passthrough_env_rejects_secret(self):
        with pytest.raises(ValidationError, match="looks like a secret"):
            ExecutorSection(passthrough_env=["AWS_SECRET_ACCESS_KEY"])

    def test_passthrough_env_rejects_token(self):
        with pytest.raises(ValidationError, match="looks like a secret"):
            ExecutorSection(passthrough_env=["ANTHROPIC_API_KEY"])

    def test_passthrough_env_rejects_password(self):
        with pytest.raises(ValidationError, match="looks like a secret"):
            ExecutorSection(passthrough_env=["DB_PASSWORD"])


class TestRedisSection:
    def test_valid_url(self):
        s = RedisSection(url="redis://redis:6379/0")
        assert s.url == "redis://redis:6379/0"

    def test_rediss_url(self):
        s = RedisSection(url="rediss://secure-host:6380/1")
        assert s.url.startswith("rediss://")

    def test_invalid_url(self):
        with pytest.raises(ValidationError, match="redis://"):
            RedisSection(url="http://not-redis:6379")

    def test_empty_prefix(self):
        with pytest.raises(ValidationError, match="key_prefix"):
            RedisSection(key_prefix="")

    def test_sentinel_mode_requires_nodes(self):
        with pytest.raises(ValidationError, match="sentinel_nodes"):
            RedisSection(mode="sentinel", sentinel_service_name="nomos-master")

    def test_sentinel_mode_accepts_nodes(self):
        s = RedisSection(
            mode="sentinel",
            sentinel_service_name="nomos-master",
            sentinel_nodes=["redis-sentinel:26379"],
        )
        assert s.mode == "sentinel"


class TestFilesystemSection:
    def test_defaults(self):
        s = FilesystemSection()
        assert s.allowed_working_dirs == ["/workspace"]

    def test_relative_path_rejected(self):
        with pytest.raises(ValidationError, match="absolute path"):
            FilesystemSection(allowed_working_dirs=["workspace"])

    def test_empty_dirs_rejected(self):
        with pytest.raises(ValidationError, match="must not be empty"):
            FilesystemSection(allowed_working_dirs=[])

    def test_empty_denied_path_rejected(self):
        with pytest.raises(ValidationError, match="non-empty"):
            FilesystemSection(denied_paths=["", "/etc/shadow"])

    def test_valid_denied_paths(self):
        s = FilesystemSection(denied_paths=["/etc/shadow", ".ssh"])
        assert len(s.denied_paths) == 2


class TestToolsSection:
    def test_empty_allowed_is_valid(self):
        s = ToolsSection(allowed=[])
        assert s.allowed == []

    def test_path_in_tool_name_rejected(self):
        with pytest.raises(ValidationError, match="basenames"):
            ToolsSection(allowed=["/usr/bin/echo"])

    def test_empty_tool_name_rejected(self):
        with pytest.raises(ValidationError, match="non-empty"):
            ToolsSection(allowed=["echo", ""])

    def test_valid_denied_args(self):
        s = ToolsSection(denied_args={"rm": [r"-rf\s+/"]})
        assert "rm" in s.denied_args

    def test_invalid_regex_in_denied_args(self):
        with pytest.raises(ValidationError, match="Invalid regex"):
            ToolsSection(denied_args={"rm": ["[invalid"]})


class TestSecuritySection:
    def test_empty_patterns_valid(self):
        s = SecuritySection(blocked_patterns=[])
        assert s.blocked_patterns == []

    def test_valid_pattern(self):
        s = SecuritySection(blocked_patterns=[r"\bsudo\b"])
        assert len(s.blocked_patterns) == 1

    def test_invalid_regex_rejected(self):
        with pytest.raises(ValidationError, match="Invalid regex"):
            SecuritySection(blocked_patterns=["[broken"])


class TestExecutorYamlConfig:
    def test_empty_dict_uses_defaults(self):
        cfg = ExecutorYamlConfig.model_validate({})
        assert cfg.executor.listen_port == 9902
        assert cfg.filesystem.allowed_working_dirs == ["/workspace"]

    def test_full_valid_config(self):
        raw = {
            "executor": {"listen_port": 8080, "max_command_timeout": 30},
            "council": {"url": "http://council:9901"},
            "redis": {"url": "redis://redis:6379/0"},
            "filesystem": {
                "allowed_working_dirs": ["/workspace", "/tmp"],
                "denied_paths": ["/etc/shadow"],
            },
            "tools": {
                "allowed": ["echo", "cat"],
                "denied_args": {"rm": [r"-rf\s+/"]},
            },
            "security": {"blocked_patterns": [r"\bsudo\b"]},
        }
        cfg = ExecutorYamlConfig.model_validate(raw)
        assert cfg.executor.listen_port == 8080
        assert cfg.tools.allowed == ["echo", "cat"]

    def test_extra_keys_rejected(self):
        with pytest.raises(ValidationError, match="extra"):
            ExecutorYamlConfig.model_validate({"unknown_section": {}})

    def test_nested_validation_error(self):
        with pytest.raises(ValidationError):
            ExecutorYamlConfig.model_validate({"executor": {"listen_port": -1}})


class TestFromYamlValidation:
    """Test that from_yaml() raises on invalid config."""

    def test_invalid_yaml_raises_valueerror(self, tmp_path):
        bad_config = tmp_path / "bad.yaml"
        bad_config.write_text("executor:\n  listen_port: -1\n")
        with pytest.raises(ValueError, match="Invalid executor config"):
            ExecutorConfig.from_yaml(str(bad_config))

    def test_bad_regex_raises_valueerror(self, tmp_path):
        bad_config = tmp_path / "bad_regex.yaml"
        bad_config.write_text("security:\n  blocked_patterns:\n    - '[broken'\n")
        with pytest.raises(ValueError, match="Invalid regex"):
            ExecutorConfig.from_yaml(str(bad_config))

    def test_relative_dir_raises_valueerror(self, tmp_path):
        bad_config = tmp_path / "bad_dirs.yaml"
        bad_config.write_text("filesystem:\n  allowed_working_dirs:\n    - relative/path\n")
        with pytest.raises(ValueError, match="absolute path"):
            ExecutorConfig.from_yaml(str(bad_config))

    def test_valid_yaml_loads(self, tmp_path):
        good_config = tmp_path / "good.yaml"
        good_config.write_text(
            "executor:\n"
            "  listen_port: 9902\n"
            "  max_command_timeout: 30\n"
            "filesystem:\n"
            "  allowed_working_dirs:\n"
            "    - /workspace\n"
            "tools:\n"
            "  allowed:\n"
            "    - echo\n"
            "    - cat\n"
        )
        cfg = ExecutorConfig.from_yaml(str(good_config))
        assert cfg.max_command_timeout == 30
        assert cfg.allowed_tools == ["echo", "cat"]
        assert "/workspace" in cfg.allowed_working_directories

    def test_extra_key_in_yaml_raises(self, tmp_path):
        bad_config = tmp_path / "extra.yaml"
        bad_config.write_text("executor:\n  listen_port: 9902\nbogus_section:\n  foo: bar\n")
        with pytest.raises(ValueError, match="Invalid executor config"):
            ExecutorConfig.from_yaml(str(bad_config))

    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError, match="not found"):
            ExecutorConfig.from_yaml("/nonexistent/path.yaml")
