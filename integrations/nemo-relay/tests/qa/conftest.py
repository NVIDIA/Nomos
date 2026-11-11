# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""QA fixtures. Helpers live in qa_support.py; nothing here reads the implementation."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import qa_support as qa  # noqa: E402


@pytest.fixture
def qa_config() -> dict:
    return qa.raw_config()


@pytest.fixture
def qa_council() -> qa.FakeCouncil:
    return qa.FakeCouncil()


@pytest.fixture
def qa_executor() -> qa.FakeExecutor:
    return qa.FakeExecutor()


@pytest.fixture
def qa_next() -> qa.FakeToolNext:
    return qa.FakeToolNext()


@pytest.fixture
def qa_context() -> qa.FakeContext:
    return qa.FakeContext()
