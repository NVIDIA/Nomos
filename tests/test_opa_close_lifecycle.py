"""OPA transport cleanup remains retryable through service/registry ownership."""

import asyncio

import pytest

from nomos.config.settings import OPAConfig, load_council_catalog
from nomos.nat.functions import CouncilService
from nomos.nat.registry import CouncilRegistry
from nomos.opa.client import OPAEvaluationError, OPAHttpClient
from tests.test_council_registry import _write_catalog
from tests.test_service_resource_lifecycle import _settings


class FailOnceTransport:
    def __init__(self, failure):
        self.failure = failure
        self.calls = 0
        self.closed = False

    async def aclose(self):
        self.calls += 1
        if self.calls == 1:
            raise self.failure
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("close failed"), asyncio.CancelledError()])
async def test_opa_failed_close_retries_without_reopening_admission(failure):
    opa = OPAHttpClient(OPAConfig())
    transport = FailOnceTransport(failure)
    opa._client = transport
    with pytest.raises(type(failure)):
        await opa.aclose()
    with pytest.raises(OPAEvaluationError, match="closed"):
        await opa.evaluate({})
    await opa.aclose()
    await opa.aclose()
    assert transport.calls == 2 and transport.closed


@pytest.mark.asyncio
async def test_registry_retries_actual_service_opa_transport_close(tmp_path, monkeypatch):
    service = CouncilService(_settings())
    opa = OPAHttpClient(OPAConfig())
    transport = FailOnceTransport(RuntimeError("close failed"))
    opa._client = transport
    service._opa_client = opa
    monkeypatch.setattr("nomos.nat.registry.CouncilService", lambda *_args, **_kwargs: service)
    path = _write_catalog(tmp_path)
    registry = CouncilRegistry(load_council_catalog(str(path)), path)
    assert await registry.get("council-a") is service
    with pytest.raises(RuntimeError, match="failed to close"):
        await registry.aclose()
    assert service in registry._retired and transport.calls == 1 and not transport.closed
    with pytest.raises(OPAEvaluationError, match="closed"):
        await opa.evaluate({})
    await registry.aclose()
    assert service not in registry._retired and transport.closed and transport.calls == 2
    await registry.aclose()
    assert transport.calls == 2
