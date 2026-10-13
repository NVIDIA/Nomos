# SPDX-License-Identifier: Apache-2.0
import pytest
from fastapi.testclient import TestClient

from assurance.demo_source import create_app
from nomos.level_0.crypto import KeyPair, Signer
from nomos.level_1.ledger.chain import LedgerChain


@pytest.mark.parametrize(
    "scenario, count", [("denial", 3), ("retry-loop", 9), ("verification", 1), ("benign", 2)]
)
def test_live_scenario_is_bounded_authenticated_and_durably_idempotent(scenario, count):
    chain = LedgerChain(signer=Signer(KeyPair.generate()), chain_id="demo")
    chain.initialize("tests")

    def app():
        return create_app(chain=chain, token="reader", fixture_token="operator", council_id="demo")

    path = "/fixtures/run"
    request = {"scenario": scenario, "run_id": "test-1"}
    auth = {"Authorization": "Bearer operator"}
    with TestClient(app()) as client:
        assert client.post(path, json=request).status_code == 401
        assert (
            client.post(path, json=request, headers={"Authorization": "Bearer reader"}).status_code
            == 401
        )
        result = client.post(path, json=request, headers=auth)
        assert result.status_code == 200
        first = result.json()
        assert first["appended"] == count
        assert first["sequences"] == list(range(1, count + 1))
        assert first["synthetic"] is True
        for seq in first["sequences"]:
            payload = chain.get(seq).payload
            assert payload["agent_id"] == first["agent_id"]
            assert payload["synthetic"] is True
            assert payload["test_run_id"] == "test-1"
            if "council_id" in payload:
                assert payload["council_id"] == "demo"
    # A source restart must not duplicate an earlier operator request.
    with TestClient(app()) as client:
        repeated = client.post(path, json=request, headers=auth).json()
        assert repeated == {**first, "appended": 0}
        second = client.post(path, json={**request, "run_id": "test-2"}, headers=auth).json()
        assert second["appended"] == count
        assert second["agent_id"] != first["agent_id"]
        assert set(first["sequences"]).isdisjoint(second["sequences"])
        assert chain.length() == 1 + count * 2


@pytest.mark.parametrize(
    "body",
    [
        {"scenario": "unknown", "run_id": "test-1"},
        {"scenario": "denial", "run_id": "../bad"},
        {"scenario": "denial", "run_id": "x" * 41},
        {"scenario": "denial", "run_id": ""},
        {"scenario": "denial", "run_id": "test", "command": "arbitrary command"},
    ],
)
def test_live_scenario_rejects_invalid_or_arbitrary_inputs(body):
    chain = LedgerChain(signer=Signer(KeyPair.generate()), chain_id="demo")
    chain.initialize("tests")
    app = create_app(chain=chain, token="reader", fixture_token="operator", council_id="demo")
    with TestClient(app) as client:
        response = client.post(
            "/fixtures/run", json=body, headers={"Authorization": "Bearer operator"}
        )
        assert response.status_code == 422
        assert chain.length() == 1


def test_fixture_auth_idempotency_and_tampering_does_not_modify_ledger():
    chain = LedgerChain(signer=Signer(KeyPair.generate()), chain_id="demo")
    chain.initialize("tests")
    app = create_app(
        chain=chain, token="fixture-token", fixture_token="operator-token", council_id="demo"
    )
    auth = {"Authorization": "Bearer fixture-token"}
    operator = {"Authorization": "Bearer operator-token"}
    with TestClient(app) as client:
        assert client.post("/fixtures/seed").status_code == 401
        assert client.post("/fixtures/fault", json={"enabled": True}).status_code == 401
        assert client.post("/fixtures/seed", headers=auth).status_code == 401
        first = client.post("/fixtures/seed", headers=operator).json()
        assert first["appended"] == 15
        assert client.post("/fixtures/seed", headers=operator).json()["appended"] == 0
        assert chain.length() == 16
        path = "/debug/tools/call/council_ledger_get"
        args = {"council_id": "demo", "sequence": 15}
        clean = client.post(path, json=args, headers=auth).json()["entry"]
        client.post("/fixtures/fault", json={"enabled": True}, headers=operator)
        corrupt = client.post(path, json=args, headers=auth).json()["entry"]
        assert corrupt["payload"] != clean["payload"]
        assert chain.get(15).to_dict() == clean
        client.post("/fixtures/fault", json={"enabled": False}, headers=operator)
        assert client.post(path, json=args, headers=auth).json()["entry"] == clean
        assert (
            client.post(path, json={**args, "council_id": "wrong"}, headers=auth).status_code == 404
        )
