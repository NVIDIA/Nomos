# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


"""OpenShell-backed WritEnforcer."""

import hashlib
import time

from executors.docker.enforcer import WritEnforcer
from trusted_executor_contract import ExecutionResult, WritOfExecution

from .client import OpenShellClient
from .config import OpenShellExecutorConfig


class OpenShellEnforcer(WritEnforcer):
    """Verifies writs locally, then delegates execution to OpenShell."""

    def __init__(
        self,
        public_key: bytes,
        nonce_store,
        config: OpenShellExecutorConfig,
        additional_keys: dict | None = None,
    ):
        super().__init__(public_key, nonce_store, config, additional_keys)
        self._openshell_config = config
        self._openshell = OpenShellClient(
            config.openshell_endpoint,
            config.openshell_request_timeout,
        )

    def _unpack_packaged_files(self, writ: WritOfExecution) -> ExecutionResult | None:
        """OpenShell receives packaged files directly, so do not write them locally."""
        return None

    async def _execute_command(self, writ: WritOfExecution) -> ExecutionResult:
        timeout = min(
            self._openshell_config.max_command_timeout,
            int(writ.expires_at - time.time()),
        )
        if timeout <= 0:
            return ExecutionResult(
                writ_id=writ.writ_id,
                error="Writ expired before execution could start",
                verification_stage="temporal",
            )

        started = time.time()
        openshell_result = await self._openshell.execute(
            command=writ.command,
            working_directory=writ.working_directory,
            timeout_seconds=timeout,
            packaged_files=writ.packaged_files,
            metadata={
                "writ_id": writ.writ_id,
                "intent_hash": writ.intent_hash,
                "target_agent_id": writ.target_agent_id,
                "council_id": writ.council_id,
            },
        )
        duration = time.time() - started

        if openshell_result.error:
            return ExecutionResult(
                writ_id=writ.writ_id,
                success=False,
                exit_code=openshell_result.exit_code,
                stdout=openshell_result.stdout,
                stderr=openshell_result.stderr,
                duration_seconds=duration,
                error=openshell_result.error,
                verification_stage="execution",
                executed_command_shell=openshell_result.shell,
                executed_command_argv=openshell_result.argv or [],
            )

        stdout_hash = hashlib.sha256(openshell_result.stdout.encode()).hexdigest()
        stderr_hash = hashlib.sha256(openshell_result.stderr.encode()).hexdigest()
        await self._nonce_store.report_outcome(
            writ_id=writ.writ_id,
            exit_code=openshell_result.exit_code or 0,
            stdout_hash=stdout_hash,
            stderr_hash=stderr_hash,
        )
        return ExecutionResult(
            writ_id=writ.writ_id,
            success=openshell_result.exit_code == 0,
            exit_code=openshell_result.exit_code,
            stdout=openshell_result.stdout,
            stderr=openshell_result.stderr,
            duration_seconds=duration,
            verification_stage="passed",
            executed_command_shell=openshell_result.shell,
            executed_command_argv=openshell_result.argv or [],
        )
