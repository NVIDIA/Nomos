"""Deployment-profile preflight helpers shared by Nomos services.

The helpers are intentionally inert for the default development profile. Service
startup code can call them later to enforce Alpha-only fail-closed checks without
changing local development behavior.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatchcase


class DeployProfile(StrEnum):
    """Supported deployment profiles."""

    DEV = "dev"
    ALPHA = "alpha"


class DeploymentPreflightError(ValueError):
    """Raised when Alpha deployment preflight checks fail."""

    def __init__(self, issues: Iterable[str]):
        self.issues = tuple(issues)
        super().__init__("Alpha deployment preflight failed: " + "; ".join(self.issues))


@dataclass(frozen=True)
class SecretCheck:
    """Secret-like value that must be non-empty and non-default in Alpha."""

    name: str
    value: str | None
    required: bool = True


@dataclass(frozen=True)
class FlagCheck:
    """Boolean flag that must match an Alpha-safe value."""

    name: str
    value: bool
    expected: bool


@dataclass(frozen=True)
class BindCheck:
    """Network bind address that must not expose an internal service in Alpha."""

    name: str
    value: str | None
    allow_public: bool = False


_PROFILE_ALIASES = {
    "dev": DeployProfile.DEV,
    "development": DeployProfile.DEV,
    "local": DeployProfile.DEV,
    "alpha": DeployProfile.ALPHA,
    "prod": DeployProfile.ALPHA,
    "production": DeployProfile.ALPHA,
}

_KNOWN_INSECURE_SECRET_PATTERNS = (
    "admin",
    "change-me",
    "changeme",
    "kind-*-change-me",
    "nomos-dev",
    "nomos-dev-admin",
    "nomos-dev-telemetry",
    "replace-me",
    "replace-with-*",
)

_PUBLIC_BIND_HOSTS = {"", "*", "::", "[::]", "0.0.0.0"}  # noqa: S104


def normalize_deploy_profile(value: str | DeployProfile | None) -> DeployProfile:
    """Normalize a deployment profile value.

    Missing or empty values preserve current behavior by selecting development.
    """

    if isinstance(value, DeployProfile):
        return value
    if value is None or not value.strip():
        return DeployProfile.DEV
    normalized = value.strip().lower()
    try:
        return _PROFILE_ALIASES[normalized]
    except KeyError as exc:
        allowed = ", ".join(sorted(_PROFILE_ALIASES))
        raise ValueError(
            f"unknown deployment profile {value!r}; expected one of: {allowed}"
        ) from exc


def is_known_insecure_secret(value: str | None) -> bool:
    """Return true when a value matches a known development/default secret."""

    if value is None:
        return False
    normalized = value.strip().lower()
    if not normalized:
        return False
    return any(fnmatchcase(normalized, pattern) for pattern in _KNOWN_INSECURE_SECRET_PATTERNS)


def _bind_host(value: str | None) -> str:
    if value is None:
        return ""
    stripped = value.strip()
    if not stripped:
        return ""
    if stripped.startswith("[") and "]" in stripped:
        return stripped[: stripped.index("]") + 1]
    if stripped.count(":") == 1:
        host, _, _port = stripped.partition(":")
        return host
    return stripped


def is_public_bind_addr(value: str | None) -> bool:
    """Return true when an address binds an internal service publicly."""

    return _bind_host(value) in _PUBLIC_BIND_HOSTS


def validate_deployment_preflight(
    *,
    profile: str | DeployProfile | None,
    secrets: Iterable[SecretCheck] = (),
    flags: Iterable[FlagCheck] = (),
    binds: Iterable[BindCheck] = (),
) -> None:
    """Validate deployment settings for the requested profile.

    The development profile intentionally performs no checks. Alpha callers get
    a single exception containing all discovered issues so startup messages can
    be precise without forcing operators through one failure at a time.
    """

    if normalize_deploy_profile(profile) is DeployProfile.DEV:
        return

    issues: list[str] = []
    for secret in secrets:
        value = (secret.value or "").strip()
        if secret.required and not value:
            issues.append(f"{secret.name} is required")
        elif value and is_known_insecure_secret(value):
            issues.append(f"{secret.name} uses a known development default")

    for flag in flags:
        if flag.value is not flag.expected:
            issues.append(f"{flag.name} must be {str(flag.expected).lower()}")

    for bind in binds:
        if not bind.allow_public and is_public_bind_addr(bind.value):
            issues.append(f"{bind.name} must not bind publicly")

    if issues:
        raise DeploymentPreflightError(issues)
