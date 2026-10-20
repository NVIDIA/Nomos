# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared runtime utilities for Nomos services."""

from .deployment import (
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
from .http_server import ThreadedTLSHTTPServer
from .tls import (
    CertificateIdentity,
    ClientTLSConfig,
    ServerTLSConfig,
    TLSConfigurationError,
    certificate_identity,
    listener_tls_mode,
    require_secure_url,
    require_strict_client,
    require_strict_listener,
    require_strict_postgres,
    strict_transport_profile,
    urlopen_no_redirect,
)

__all__ = [
    "BindCheck",
    "CertificateIdentity",
    "ClientTLSConfig",
    "DeployProfile",
    "DeploymentPreflightError",
    "FlagCheck",
    "SecretCheck",
    "ServerTLSConfig",
    "TLSConfigurationError",
    "ThreadedTLSHTTPServer",
    "certificate_identity",
    "is_known_insecure_secret",
    "is_public_bind_addr",
    "listener_tls_mode",
    "normalize_deploy_profile",
    "require_secure_url",
    "require_strict_client",
    "require_strict_listener",
    "require_strict_postgres",
    "strict_transport_profile",
    "urlopen_no_redirect",
    "validate_deployment_preflight",
]
