# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


"""Configuration for the OpenShell trusted executor."""

import os
from dataclasses import dataclass

import yaml

from executors.docker.config import ExecutorConfig
from executors.docker.config_schema import ExecutorYamlConfig


@dataclass
class OpenShellExecutorConfig(ExecutorConfig):
    """Docker executor config plus OpenShell backend settings."""

    openshell_endpoint: str = ""
    openshell_request_timeout: int = 30

    @classmethod
    def load(cls) -> "OpenShellExecutorConfig":
        base = ExecutorConfig.load()
        endpoint = os.environ.get("OPENSHELL_ENDPOINT", "")
        request_timeout = int(os.environ.get("OPENSHELL_REQUEST_TIMEOUT", "30"))

        config_path = os.environ.get("EXECUTOR_CONFIG", "")
        if config_path:
            with open(os.path.expanduser(config_path)) as f:
                raw = yaml.safe_load(f) or {}
            validated = ExecutorYamlConfig.model_validate(raw)
            endpoint = endpoint or validated.openshell.endpoint
            request_timeout = int(
                os.environ.get(
                    "OPENSHELL_REQUEST_TIMEOUT",
                    str(validated.openshell.request_timeout),
                )
            )

        return cls(
            **base.__dict__,
            openshell_endpoint=endpoint,
            openshell_request_timeout=request_timeout,
        )
