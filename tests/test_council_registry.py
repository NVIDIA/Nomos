import pytest
import yaml

from nomos.config.settings import load_council_catalog
from nomos.nat.functions import AuthorizeInput, ProposeIntentInput
from nomos.nat.registry import CouncilRegistry


def _write_catalog(tmp_path):
    path = tmp_path / "multi.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "default_council_id": "council-a",
                "shared": {
                    "require_auth": False,
                    "require_council_membership": True,
                    "llm_backends": {},
                    "members": [],
                    "opa": {"enabled": False},
                    "council_key_path": "",
                    "ledger_path": "",
                    "redis": {"host": ""},
                },
                "councils": {
                    "council-a": {},
                    "council-b": {},
                },
            }
        )
    )
    return path


@pytest.mark.asyncio
async def test_registry_routes_to_default_council_for_v1(tmp_path):
    registry = CouncilRegistry(load_council_catalog(str(_write_catalog(tmp_path))))

    service, normalized = await registry.service_for_propose(
        ProposeIntentInput(
            agent_id="agent-1",
            description="Read workspace contents for diagnostics",
            target_resource="/workspace",
            justification="Need to verify workspace state",
        )
    )

    assert service.council_id == "council-a"
    assert normalized.council_id == "council-a"
    assert normalized.api_version == "v1"


@pytest.mark.asyncio
async def test_registry_routes_to_explicit_council_for_v2(tmp_path):
    registry = CouncilRegistry(load_council_catalog(str(_write_catalog(tmp_path))))

    service, normalized = await registry.service_for_authorize(
        AuthorizeInput(
            agent_id="agent-1",
            council_id="council-b",
            command="/bin/ls /workspace",
            working_directory="/workspace",
            description="Read workspace contents for diagnostics",
            target_resource="/workspace",
            justification="Need to verify workspace state",
        ),
        require_explicit=True,
    )

    assert service.council_id == "council-b"
    assert normalized.council_id == "council-b"
    assert normalized.api_version == "v2"


@pytest.mark.asyncio
async def test_registry_requires_council_id_for_v2(tmp_path):
    registry = CouncilRegistry(load_council_catalog(str(_write_catalog(tmp_path))))

    with pytest.raises(ValueError, match="council_id is required"):
        await registry.service_for_propose(
            ProposeIntentInput(
                agent_id="agent-1",
                description="Read workspace contents for diagnostics",
                target_resource="/workspace",
                justification="Need to verify workspace state",
            ),
            require_explicit=True,
        )
