"""
Tests for configuration system.
"""

import os
import tempfile

import pytest
import yaml
from pydantic import ValidationError

from nomos.config.settings import (
    ChairmanConfig,
    CouncilMemberConfig,
    CouncilSettings,
    LLMBackendConfig,
    LLMProvider,
    OPAConfig,
    RedisConfig,
    load_config,
    load_council_catalog,
)
from nomos_runtime import DeploymentPreflightError


class TestLLMBackendConfig:
    """Tests for LLMBackendConfig."""

    def test_valid_openai_config(self):
        config = LLMBackendConfig(
            provider=LLMProvider.OPENAI,
            model="gpt-4o",
            api_key="sk-test-key",
        )
        assert config.provider == LLMProvider.OPENAI
        assert config.model == "gpt-4o"

    def test_valid_anthropic_config(self):
        config = LLMBackendConfig(
            provider=LLMProvider.ANTHROPIC,
            model="claude-sonnet-4-20250514",
            api_key="sk-ant-test",
        )
        assert config.provider == LLMProvider.ANTHROPIC

    def test_valid_nim_config(self):
        config = LLMBackendConfig(
            provider=LLMProvider.NIM,
            model="nvidia/nemotron-3-nano-30b-a3b",
            api_key="nvapi-test",
        )
        assert config.provider == LLMProvider.NIM

    def test_throughput_defaults(self):
        config = LLMBackendConfig(
            provider=LLMProvider.NV_INFERENCE,
            model="azure/anthropic/claude-sonnet-4-6",
            api_key="nvapi-test",
        )
        assert config.max_concurrent == 4
        assert config.request_timeout_seconds == 30.0
        assert config.sdk_max_retries == 0
        assert config.queue_reject_threshold == 8

    def test_max_concurrent_must_be_positive(self):
        with pytest.raises(ValidationError):
            LLMBackendConfig(
                provider=LLMProvider.NV_INFERENCE,
                model="m",
                api_key="k",
                max_concurrent=0,
            )

    def test_env_var_resolution(self):
        os.environ["TEST_API_KEY"] = "resolved-key"
        try:
            config = LLMBackendConfig(
                provider=LLMProvider.OPENAI,
                model="gpt-4o",
                api_key="${TEST_API_KEY}",
            )
            assert config.api_key == "resolved-key"
        finally:
            del os.environ["TEST_API_KEY"]

    def test_env_var_missing_raises(self):
        if "NONEXISTENT_KEY_12345" in os.environ:
            del os.environ["NONEXISTENT_KEY_12345"]
        with pytest.raises(ValidationError) as exc:
            LLMBackendConfig(
                provider=LLMProvider.OPENAI,
                model="gpt-4o",
                api_key="${NONEXISTENT_KEY_12345}",
            )
        assert "NONEXISTENT_KEY_12345" in str(exc.value)

    def test_temperature_bounds(self):
        with pytest.raises(ValidationError):
            LLMBackendConfig(
                provider=LLMProvider.OPENAI,
                model="gpt-4o",
                api_key="test",
                temperature=3.0,
            )


class TestCouncilMemberConfig:
    """Tests for CouncilMemberConfig."""

    def test_valid_member(self):
        member = CouncilMemberConfig(
            id="security-reviewer",
            backend="anthropic-main",
            weight=2,
            display_name="Security Reviewer",
            priorities=["Check for privilege escalation"],
        )
        assert member.id == "security-reviewer"
        assert member.weight == 2

    def test_priorities_required(self):
        with pytest.raises(ValidationError):
            CouncilMemberConfig(
                id="empty-priorities",
                backend="test",
                priorities=[],
            )

    def test_system_prompt_from_priorities(self):
        member = CouncilMemberConfig(
            id="test",
            backend="test",
            priorities=["Security first", "Check blast radius"],
            evaluation_criteria={"data_leak": "Could this leak data?"},
        )
        prompt = member.build_system_prompt()
        assert "Security first" in prompt
        assert "Check blast radius" in prompt
        assert "data_leak" in prompt
        assert "approve" in prompt.lower() or "APPROVED" in prompt
        assert "Certified executor evidence" in prompt
        assert "agent-asserted claims" in prompt

    def test_system_prompt_override(self):
        member = CouncilMemberConfig(
            id="test",
            backend="test",
            priorities=["Unused priority"],
            system_prompt_override="Custom prompt here",
        )
        assert member.build_system_prompt() == "Custom prompt here"


class TestRedisConfigDefaults:
    def test_sentinel_env_reference_parses_nodes(self, monkeypatch):
        monkeypatch.setenv("TEST_REDIS_MODE", "sentinel")
        monkeypatch.setenv("TEST_SENTINEL_NODES", "sentinel-a:26379,sentinel-b:26379")

        config = RedisConfig(
            mode="${TEST_REDIS_MODE}",
            sentinel_service_name="nomos-master",
            sentinel_nodes="${TEST_SENTINEL_NODES}",
        )

        assert config.mode == "sentinel"
        assert config.sentinel_nodes == ["sentinel-a:26379", "sentinel-b:26379"]

    def test_partial_sentinel_config_reads_nodes_from_environment(self, monkeypatch):
        monkeypatch.setenv("COUNCIL_REDIS_MODE", "sentinel")
        monkeypatch.setenv("COUNCIL_REDIS_SENTINEL_SERVICE_NAME", "nomos-master")
        monkeypatch.setenv(
            "COUNCIL_REDIS_SENTINEL_NODES",
            "sentinel-a:26379,sentinel-b:26379",
        )

        config = RedisConfig(host="")

        assert config.mode == "sentinel"
        assert config.sentinel_service_name == "nomos-master"
        assert config.sentinel_nodes == ["sentinel-a:26379", "sentinel-b:26379"]

    def test_sentinel_mode_requires_nodes(self):
        with pytest.raises(ValidationError, match="sentinel_nodes"):
            RedisConfig(mode="sentinel", sentinel_service_name="nomos-master")


class TestCouncilSettings:
    """Tests for CouncilSettings."""

    def test_valid_settings(self):
        settings = CouncilSettings(
            council_id="test-council",
            llm_backends={
                "test-backend": LLMBackendConfig(
                    provider=LLMProvider.OPENAI,
                    model="gpt-4o",
                    api_key="test-key",
                ),
            },
            members=[
                CouncilMemberConfig(
                    id="member-1",
                    backend="test-backend",
                    priorities=["Evaluate security"],
                ),
            ],
        )
        assert settings.council_id == "test-council"
        assert len(settings.members) == 1
        assert settings.require_auth is True

    def test_llm_retry_defaults(self):
        settings = CouncilSettings(council_id="test-council")
        assert settings.llm_max_retries == 2
        assert settings.llm_retry_budget_seconds == 40.0
        assert settings.llm_max_retry_after_seconds == 10.0

    def test_member_references_invalid_backend(self):
        with pytest.raises(ValidationError) as exc:
            CouncilSettings(
                llm_backends={
                    "real-backend": LLMBackendConfig(
                        provider=LLMProvider.OPENAI,
                        model="gpt-4o",
                        api_key="test",
                    ),
                },
                members=[
                    CouncilMemberConfig(
                        id="member-1",
                        backend="nonexistent-backend",
                        priorities=["Test"],
                    ),
                ],
            )
        assert "nonexistent-backend" in str(exc.value)

    def test_default_nat_config(self):
        settings = CouncilSettings()
        assert settings.nat.host == "localhost"
        assert settings.nat.port == 9901

    def test_default_opa_config(self):
        settings = CouncilSettings()
        assert settings.opa.enabled is True
        assert settings.opa.url == "http://localhost:8181"
        assert settings.opa.decision_path == "/v1/data/nomos/intent/decision"
        assert settings.opa.policy_id == "strict-shell"
        assert settings.opa.relaxed_bare_command_words == []
        assert settings.opa.relaxed_working_dir_prefixes == []

    def test_chairman_quorum_is_opt_in(self):
        assert ChairmanConfig().allow_quorum is False
        assert ChairmanConfig(allow_quorum=True).allow_quorum is True

    def test_allow_uncertified_evidence_resolves_deployment_env_var(self, monkeypatch):
        monkeypatch.delenv("COUNCIL_ALLOW_UNCERTIFIED_EVIDENCE", raising=False)
        settings = CouncilSettings(
            allow_uncertified_evidence="${COUNCIL_ALLOW_UNCERTIFIED_EVIDENCE:-true}"
        )
        assert settings.allow_uncertified_evidence is True

        monkeypatch.setenv("COUNCIL_ALLOW_UNCERTIFIED_EVIDENCE", "false")
        settings = CouncilSettings(
            allow_uncertified_evidence="${COUNCIL_ALLOW_UNCERTIFIED_EVIDENCE:-true}"
        )
        assert settings.allow_uncertified_evidence is False

    def test_allow_uncertified_evidence_default_resolves_deployment_env_var(self, monkeypatch):
        monkeypatch.delenv("COUNCIL_ALLOW_UNCERTIFIED_EVIDENCE", raising=False)
        assert CouncilSettings().allow_uncertified_evidence is True

        monkeypatch.setenv("COUNCIL_ALLOW_UNCERTIFIED_EVIDENCE", "false")
        assert CouncilSettings().allow_uncertified_evidence is False


class TestOPAConfig:
    def test_normalizes_package_relative_decision_path(self):
        config = OPAConfig(decision_path="nomos/intent/decision")
        assert config.decision_path == "/v1/data/nomos/intent/decision"

    def test_resolves_deployment_env_vars(self, monkeypatch):
        monkeypatch.setenv("COUNCIL_OPA_ENABLED", "true")
        monkeypatch.setenv("COUNCIL_OPA_URL", "https://opa.example.internal/")
        monkeypatch.setenv("COUNCIL_OPA_TIMEOUT_MS", "1500")
        config = OPAConfig(
            enabled="${COUNCIL_OPA_ENABLED}",
            url="${COUNCIL_OPA_URL}",
            timeout_ms="${COUNCIL_OPA_TIMEOUT_MS}",
        )
        assert config.enabled is True
        assert config.url == "https://opa.example.internal"
        assert config.timeout_ms == 1500

    def test_env_default_syntax_supports_deploy_time_defaults(self):
        config = OPAConfig(
            enabled="${MISSING_OPA_ENABLED:-true}",
            url="${MISSING_OPA_URL:-http://custom-opa:8181}",
            decision_path="${MISSING_OPA_PATH:-custom/decision}",
            policy_id="${MISSING_OPA_POLICY_ID:-git-workflow-local}",
            fail_closed="${MISSING_OPA_FAIL_CLOSED:-false}",
        )
        assert config.enabled is True
        assert config.url == "http://custom-opa:8181"
        assert config.decision_path == "/v1/data/custom/decision"
        assert config.policy_id == "git-workflow-local"
        assert config.fail_closed is False

    def test_rejects_invalid_url(self):
        with pytest.raises(ValidationError, match="http"):
            OPAConfig(url="opa:8181")

    def test_rejects_empty_policy_id(self):
        with pytest.raises(ValidationError, match="policy_id"):
            OPAConfig(policy_id=" ")

    def test_normalizes_relaxed_profile_bounds(self):
        config = OPAConfig(
            policy_id="git-workflow-local",
            relaxed_bare_command_words=["git", "gh", "git"],
            relaxed_working_dir_prefixes=["/workspace/", "/workspace/projects"],
        )
        assert config.relaxed_bare_command_words == ["gh", "git"]
        assert config.relaxed_working_dir_prefixes == [
            "/workspace",
            "/workspace/projects",
        ]

    def test_rejects_invalid_relaxed_profile_bounds(self):
        with pytest.raises(ValidationError, match="command words"):
            OPAConfig(relaxed_bare_command_words=["/usr/bin/git"])
        with pytest.raises(ValidationError, match="absolute paths"):
            OPAConfig(relaxed_working_dir_prefixes=["workspace"])


class TestRedisConfig:
    def test_defaults_resolve_deployment_env_vars(self, monkeypatch):
        monkeypatch.setenv("COUNCIL_REDIS_HOST", "redis.internal")
        monkeypatch.setenv("COUNCIL_REDIS_PASSWORD", "redis-test-password")
        monkeypatch.setenv(
            "COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY",
            "redis-integrity-test-key",
        )

        config = RedisConfig()

        assert config.host == "redis.internal"
        assert config.password == "redis-test-password"  # pragma: allowlist secret
        assert config.account_integrity_key == "redis-integrity-test-key"

    def test_defaults_preserve_dev_values_without_env(self, monkeypatch):
        monkeypatch.delenv("COUNCIL_REDIS_HOST", raising=False)
        monkeypatch.delenv("COUNCIL_REDIS_PASSWORD", raising=False)
        monkeypatch.delenv("COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY", raising=False)

        config = RedisConfig()

        assert config.host == "localhost"
        assert config.password == ""
        assert config.account_integrity_key == ""


class TestLoadConfig:
    """Tests for YAML config loading."""

    def test_load_valid_yaml(self):
        config_data = {
            "council_id": "yaml-council",
            "require_justification": True,
            "llm_backends": {
                "test-backend": {
                    "provider": "openai",
                    "model": "gpt-4o",
                    "api_key": "test-key-direct",
                },
            },
            "members": [
                {
                    "id": "reviewer-1",
                    "backend": "test-backend",
                    "priorities": ["Evaluate security implications"],
                },
            ],
            "nat": {
                "host": "0.0.0.0",  # noqa: S104
                "port": 8080,
            },
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(config_data, f)
            f.flush()
            settings = load_config(f.name)

        assert settings.council_id == "yaml-council"
        assert settings.nat.port == 8080
        assert len(settings.members) == 1
        assert settings.members[0].id == "reviewer-1"

        os.unlink(f.name)

    def test_load_nonexistent_file(self):
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/path/config.yaml")

    def test_load_single_council_as_catalog(self):
        config_data = {
            "council_id": "single-council",
            "require_auth": False,
            "llm_backends": {},
            "members": [],
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(config_data, f)
            f.flush()
            catalog = load_council_catalog(f.name)

        assert catalog.default_council_id == "single-council"
        assert sorted(catalog.councils) == ["single-council"]

        os.unlink(f.name)

    def test_load_config_dev_profile_preserves_current_defaults(self, monkeypatch):
        monkeypatch.delenv("NOMOS_DEPLOY_PROFILE", raising=False)
        config_data = {
            "council_id": "dev-council",
            "allow_uncertified_evidence": True,
            "redis": {
                "password": "nomos-dev",  # pragma: allowlist secret
                "account_integrity_key": "",
            },
            "nat": {"host": "0.0.0.0"},  # noqa: S104
            "llm_backends": {
                "test-backend": {
                    "provider": "openai",
                    "model": "gpt-4o",
                    "api_key": "",
                }
            },
            "members": [
                {
                    "id": "reviewer",
                    "backend": "test-backend",
                    "priorities": ["review"],
                }
            ],
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(config_data, f)
            f.flush()
            settings = load_config(f.name)

        assert settings.council_id == "dev-council"
        os.unlink(f.name)

    def test_load_config_alpha_profile_rejects_unsafe_defaults(self, monkeypatch):
        monkeypatch.setenv("NOMOS_DEPLOY_PROFILE", "alpha")
        monkeypatch.delenv("NOMOS_ALPHA_ALLOW_PUBLIC_BIND", raising=False)
        config_data = {
            "council_id": "alpha-council",
            "allow_uncertified_evidence": True,
            "redis": {
                "password": "nomos-dev",  # pragma: allowlist secret
                "account_integrity_key": "",
            },
            "nat": {"host": "0.0.0.0"},  # noqa: S104
            "llm_backends": {
                "test-backend": {
                    "provider": "openai",
                    "model": "gpt-4o",
                    "api_key": "",
                }
            },
            "members": [
                {
                    "id": "reviewer",
                    "backend": "test-backend",
                    "priorities": ["review"],
                }
            ],
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(config_data, f)
            f.flush()
            with pytest.raises(DeploymentPreflightError) as exc:
                load_config(f.name)

        assert exc.value.issues == (
            "redis.password uses a known development default",
            "redis.account_integrity_key is required",
            "llm_backends.test-backend.api_key is required",
            "nat.host must not bind publicly",
        )
        os.unlink(f.name)

    def test_load_config_alpha_profile_allows_explicit_public_bind_override(self, monkeypatch):
        monkeypatch.setenv("NOMOS_DEPLOY_PROFILE", "alpha")
        monkeypatch.setenv("NOMOS_ALPHA_ALLOW_PUBLIC_BIND", "true")
        config_data = {
            "council_id": "alpha-council",
            "allow_uncertified_evidence": True,
            "redis": {
                "password": "generated-redis-password",  # pragma: allowlist secret
                "account_integrity_key": "generated-account-integrity-key",
            },
            "nat": {"host": "0.0.0.0"},  # noqa: S104
            "llm_backends": {
                "test-backend": {
                    "provider": "openai",
                    "model": "gpt-4o",
                    "api_key": "configured-api-key",  # pragma: allowlist secret
                }
            },
            "members": [
                {
                    "id": "reviewer",
                    "backend": "test-backend",
                    "priorities": ["review"],
                }
            ],
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(config_data, f)
            f.flush()
            settings = load_config(f.name)

        assert settings.nat.host == "0.0.0.0"  # noqa: S104
        os.unlink(f.name)

    def test_load_config_alpha_profile_accepts_safe_values(self, monkeypatch):
        monkeypatch.setenv("NOMOS_DEPLOY_PROFILE", "alpha")
        config_data = {
            "council_id": "alpha-council",
            "allow_uncertified_evidence": True,
            "redis": {
                "password": "generated-redis-password",  # pragma: allowlist secret
                "account_integrity_key": "generated-account-integrity-key",
            },
            "nat": {"host": "127.0.0.1"},
            "llm_backends": {
                "test-backend": {
                    "provider": "openai",
                    "model": "gpt-4o",
                    "api_key": "configured-api-key",  # pragma: allowlist secret
                }
            },
            "members": [
                {
                    "id": "reviewer",
                    "backend": "test-backend",
                    "priorities": ["review"],
                }
            ],
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(config_data, f)
            f.flush()
            settings = load_config(f.name)

        assert settings.council_id == "alpha-council"
        os.unlink(f.name)

    def test_load_config_alpha_profile_uses_env_default_for_omitted_evidence_flag(
        self, monkeypatch
    ):
        monkeypatch.setenv("NOMOS_DEPLOY_PROFILE", "alpha")
        monkeypatch.setenv("COUNCIL_ALLOW_UNCERTIFIED_EVIDENCE", "false")
        config_data = {
            "council_id": "alpha-council",
            "redis": {
                "password": "generated-redis-password",  # pragma: allowlist secret
                "account_integrity_key": "generated-account-integrity-key",
            },
            "nat": {"host": "127.0.0.1"},
            "llm_backends": {
                "test-backend": {
                    "provider": "openai",
                    "model": "gpt-4o",
                    "api_key": "configured-api-key",  # pragma: allowlist secret
                }
            },
            "members": [
                {
                    "id": "reviewer",
                    "backend": "test-backend",
                    "priorities": ["review"],
                }
            ],
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(config_data, f)
            f.flush()
            settings = load_config(f.name)

        assert settings.allow_uncertified_evidence is False
        os.unlink(f.name)

    def test_load_multi_council_catalog_merges_shared_config(self):
        config_data = {
            "default_council_id": "council-a",
            "shared": {
                "require_auth": False,
                "require_council_membership": True,
                "llm_backends": {
                    "test-backend": {
                        "provider": "openai",
                        "model": "gpt-4o",
                        "api_key": "test-key-direct",
                    },
                },
            },
            "councils": {
                "council-a": {
                    "members": [
                        {
                            "id": "member-a",
                            "backend": "test-backend",
                            "priorities": ["Evaluate dev risk"],
                        },
                    ],
                },
                "council-b": {
                    "members": [
                        {
                            "id": "member-b",
                            "backend": "test-backend",
                            "priorities": ["Evaluate prod risk"],
                        },
                    ],
                },
            },
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(config_data, f)
            f.flush()
            catalog = load_council_catalog(f.name)

        assert catalog.default_council_id == "council-a"
        assert sorted(catalog.councils) == ["council-a", "council-b"]
        assert catalog.get("council-b").council_id == "council-b"
        assert catalog.get("council-b").members[0].id == "member-b"
        assert "test-backend" in catalog.get("council-b").llm_backends

        os.unlink(f.name)

    def test_load_multi_council_catalog_keeps_per_council_opa_profiles(self):
        config_data = {
            "default_council_id": "strict-council",
            "shared": {
                "require_auth": False,
                "llm_backends": {},
                "members": [],
                "opa": {
                    "enabled": True,
                    "policy_id": "strict-shell",
                },
            },
            "councils": {
                "strict-council": {},
                "git-council": {
                    "opa": {
                        "policy_id": "git-workflow-local",
                        "relaxed_working_dir_prefixes": ["/workspace/projects"],
                    },
                },
            },
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(config_data, f)
            f.flush()
            catalog = load_council_catalog(f.name)

        assert catalog.get("strict-council").opa.policy_id == "strict-shell"
        git_opa = catalog.get("git-council").opa
        assert git_opa.policy_id == "git-workflow-local"
        assert git_opa.relaxed_working_dir_prefixes == ["/workspace/projects"]

        os.unlink(f.name)

    def test_load_multi_council_catalog_with_config_file_refs(self):
        council_a = {
            "council_id": "council-a",
            "require_auth": False,
            "llm_backends": {},
            "members": [],
        }
        council_b = {
            "council_id": "council-b",
            "require_auth": False,
            "llm_backends": {},
            "members": [],
        }

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "a.yaml"), "w") as f:
                yaml.dump(council_a, f)
            with open(os.path.join(tmp, "b.yaml"), "w") as f:
                yaml.dump(council_b, f)
            catalog_path = os.path.join(tmp, "catalog.yaml")
            with open(catalog_path, "w") as f:
                yaml.dump(
                    {
                        "default_council_id": "council-a",
                        "councils": {
                            "council-a": {"config_file": "a.yaml"},
                            "council-b": {"config_file": "b.yaml"},
                        },
                    },
                    f,
                )

            catalog = load_council_catalog(catalog_path)

        assert catalog.default_council_id == "council-a"
        assert sorted(catalog.councils) == ["council-a", "council-b"]
        assert catalog.get("council-b").council_id == "council-b"

    def test_load_catalog_alpha_profile_uses_redis_env_defaults_for_omitted_fields(
        self, monkeypatch
    ):
        monkeypatch.setenv("NOMOS_DEPLOY_PROFILE", "alpha")
        monkeypatch.setenv("COUNCIL_ALLOW_UNCERTIFIED_EVIDENCE", "false")
        monkeypatch.setenv("COUNCIL_REDIS_PASSWORD", "redis-test-password")
        monkeypatch.setenv(
            "COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY",
            "redis-integrity-test-key",
        )

        council_a = {
            "council_id": "council-a",
            "enabled": False,
            "require_auth": False,
            "llm_backends": {},
            "members": [],
            "redis": {"host": ""},
            "nat": {"host": "127.0.0.1"},
        }

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "a.yaml"), "w") as f:
                yaml.dump(council_a, f)
            catalog_path = os.path.join(tmp, "catalog.yaml")
            with open(catalog_path, "w") as f:
                yaml.dump(
                    {
                        "default_council_id": "council-a",
                        "councils": {"council-a": {"config_file": "a.yaml"}},
                    },
                    f,
                )

            catalog = load_council_catalog(catalog_path)

        redis_password = catalog.get("council-a").redis.password
        assert redis_password == "redis-test-password"  # pragma: allowlist secret
        assert catalog.get("council-a").redis.account_integrity_key == "redis-integrity-test-key"

    def test_catalog_level_nat_overrides_profile_listener_fields(self):
        council_a = {
            "council_id": "council-a",
            "require_auth": False,
            "llm_backends": {},
            "members": [],
            "nat": {"host": "127.0.0.1", "port": 9911, "server_name": "Standalone"},
        }

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "a.yaml"), "w") as f:
                yaml.dump(council_a, f)
            catalog_path = os.path.join(tmp, "catalog.yaml")
            with open(catalog_path, "w") as f:
                yaml.dump(
                    {
                        "default_council_id": "council-a",
                        "nat": {
                            "host": "0.0.0.0",  # noqa: S104
                            "port": 9901,
                            "server_name": "Shared MCP",
                        },
                        "councils": {"council-a": {"config_file": "a.yaml"}},
                    },
                    f,
                )

            catalog = load_council_catalog(catalog_path)

        assert catalog.get("council-a").nat.host == "0.0.0.0"  # noqa: S104
        assert catalog.get("council-a").nat.port == 9901
        assert catalog.get("council-a").nat.server_name == "Shared MCP"
