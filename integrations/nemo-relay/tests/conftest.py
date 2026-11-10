# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Fixtures for the implementation tests; helpers live in ``support``."""

import pytest
from support import Context, Council, Executor, base_config

from nemo_relay_nomos import NomosPlugin


@pytest.fixture
def setup(tmp_path):
    plugins = []

    def create(status="approved", config=None, *, council=None, executor=None, **overrides):
        cfg = config or base_config(**overrides)
        c, e = council or Council(status), executor or Executor()
        p = NomosPlugin(council=c, executor=e)
        context = Context()
        p.register(context, cfg)
        plugins.append(p)
        return p, c, e, context

    yield create
    for plugin in plugins:
        plugin.close()
