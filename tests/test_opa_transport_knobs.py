# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""OPA connect deadline and keep-alive expiry are configurable and default unchanged."""

import asyncio

import pytest
from pydantic import ValidationError

from nomos.config.settings import OPAConfig
from nomos.opa.client import OPAHttpClient


def _transport(config: OPAConfig):
    async def build():
        client = OPAHttpClient(config)
        http = client._http_client()
        try:
            return http.timeout, http._transport._pool._keepalive_expiry
        finally:
            await http.aclose()

    return asyncio.run(build())


def test_defaults_keep_one_phase_deadline_and_httpx_keepalive():
    timeout, keepalive = _transport(OPAConfig(url="http://opa:8181", timeout_ms=500))
    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (0.5, 0.5, 0.5, 0.5)
    assert keepalive == 5.0


def test_connect_deadline_and_keepalive_are_independent_of_read_deadline():
    timeout, keepalive = _transport(
        OPAConfig(
            url="http://opa:8181",
            timeout_ms=500,
            connect_timeout_ms=2000,
            keepalive_expiry_seconds=60,
        )
    )
    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (2.0, 0.5, 0.5, 0.5)
    assert keepalive == 60.0


@pytest.mark.parametrize(
    "field,value",
    [("connect_timeout_ms", 0), ("connect_timeout_ms", 60001), ("keepalive_expiry_seconds", -1)],
)
def test_out_of_range_values_are_rejected(field, value):
    with pytest.raises(ValidationError):
        OPAConfig(url="http://opa:8181", **{field: value})
