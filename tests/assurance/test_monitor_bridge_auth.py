# SPDX-License-Identifier: Apache-2.0
import httpx
import pytest

from monitor.server.ledger_api import _call_tool


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [None, "test-reader-token"])
async def test_monitor_sends_only_optional_reader_credential(monkeypatch, token):
    monkeypatch.delenv("MONITOR_LEDGER_API_TOKEN", raising=False)
    monkeypatch.setenv("MONITOR_INTERNAL_TOKEN", "must-not-be-forwarded")
    if token:
        monkeypatch.setenv("MONITOR_LEDGER_API_TOKEN", token)

    def handle(request):
        assert request.headers.get("Authorization") == (f"Bearer {token}" if token else None)
        assert request.url.path == "/debug/tools/call/council_ledger_page"
        return httpx.Response(200, json={"chain_id": "ledger-test", "head_sequence": 1})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await _call_tool(client, "http://reader", "council_ledger_page", {"limit": 1})
    assert result["head_sequence"] == 1
