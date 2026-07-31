# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

from __future__ import annotations

import subprocess

from scripts import run_test_suite


def test_run_suite_decodes_timeout_output(monkeypatch, tmp_path) -> None:
    spec = run_test_suite.SuiteSpec(
        key="timeout",
        label="Timeout",
        kind="script",
        command=["timeout-command"],
        components=(),
        description="Timeout test",
    )

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=["timeout-command"],
            timeout=1,
            output=b"partial stdout",
            stderr=b"partial stderr",
        )

    monkeypatch.setattr(run_test_suite.subprocess, "run", timeout)

    result = run_test_suite._run_suite(
        spec,
        env={},
        junit_dir=tmp_path,
        timeout=1,
        require_deployed=False,
    )

    assert result.status == "failed"
    assert result.reason == "timeout"
    assert result.stdout == "partial stdout"
    assert result.stderr == "partial stderr"
    assert "partial stdout\npartial stderr" in run_test_suite._output_block(result)
