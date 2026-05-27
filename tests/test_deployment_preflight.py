import pytest

from nomos_runtime import (
    BindCheck,
    DeploymentPreflightError,
    DeployProfile,
    FlagCheck,
    SecretCheck,
    is_known_insecure_secret,
    is_public_bind_addr,
    normalize_deploy_profile,
    validate_deployment_preflight,
)


def test_normalize_deploy_profile_defaults_to_dev() -> None:
    assert normalize_deploy_profile(None) is DeployProfile.DEV
    assert normalize_deploy_profile("") is DeployProfile.DEV
    assert normalize_deploy_profile("development") is DeployProfile.DEV
    assert normalize_deploy_profile("production") is DeployProfile.ALPHA


def test_normalize_deploy_profile_rejects_unknown_value() -> None:
    with pytest.raises(ValueError, match="unknown deployment profile"):
        normalize_deploy_profile("staging")


def test_known_insecure_secret_detection() -> None:
    assert is_known_insecure_secret("nomos-dev")
    assert is_known_insecure_secret("kind-council-management-change-me")
    assert is_known_insecure_secret("replace-with-admin-password")
    assert not is_known_insecure_secret("")
    assert not is_known_insecure_secret("generated-32-byte-secret")


def test_public_bind_detection() -> None:
    assert is_public_bind_addr("0.0.0.0")  # noqa: S104
    assert is_public_bind_addr("0.0.0.0:9904")
    assert is_public_bind_addr(":9904")
    assert is_public_bind_addr("[::]:9904")
    assert not is_public_bind_addr("127.0.0.1")
    assert not is_public_bind_addr("127.0.0.1:9904")
    assert not is_public_bind_addr("redis")


def test_dev_profile_preserves_current_behavior() -> None:
    validate_deployment_preflight(
        profile="dev",
        secrets=[SecretCheck("redis_password", "nomos-dev")],
        flags=[FlagCheck("allow_uncertified_evidence", True, expected=False)],
        binds=[BindCheck("grpc_bind", "0.0.0.0:9904")],
    )


def test_alpha_preflight_collects_all_issues() -> None:
    with pytest.raises(DeploymentPreflightError) as exc:
        validate_deployment_preflight(
            profile="alpha",
            secrets=[
                SecretCheck("redis_password", "nomos-dev"),
                SecretCheck("llm_api_key", ""),
                SecretCheck("optional_blank", "", required=False),
            ],
            flags=[FlagCheck("allow_uncertified_evidence", True, expected=False)],
            binds=[BindCheck("grpc_bind", "0.0.0.0:9904")],
        )

    assert exc.value.issues == (
        "redis_password uses a known development default",
        "llm_api_key is required",
        "allow_uncertified_evidence must be false",
        "grpc_bind must not bind publicly",
    )


def test_alpha_preflight_accepts_safe_values() -> None:
    validate_deployment_preflight(
        profile="alpha",
        secrets=[
            SecretCheck("redis_password", "generated-redis-secret"),
            SecretCheck("llm_api_key", "real-key-from-secret-store"),
        ],
        flags=[FlagCheck("allow_uncertified_evidence", False, expected=False)],
        binds=[BindCheck("grpc_bind", "127.0.0.1:9904")],
    )
