import pytest
import yaml

from nomos.auth.authenticator import AgentIdentity
from nomos.config import council_catalog_store as catalog_store
from nomos.config.council_catalog_store import CouncilCatalogStore, CouncilCatalogStoreError
from nomos.config.settings import load_council_catalog
from nomos.nat.functions import AuthorizeInput, ProposeIntentInput
from nomos.nat.registry import CouncilRegistry


class StubAuthenticator:
    """Authenticator stand-in used to exercise per-council membership checks."""

    def __init__(self, identity: AgentIdentity | None):
        self._identity = identity

    async def authenticate(self, token: str) -> AgentIdentity | None:
        if not token:
            return None
        return self._identity


def _profile(
    council_id: str,
    *,
    enabled: bool = True,
    require_auth: bool = False,
    metadata: dict | None = None,
) -> dict:
    return {
        "council_id": council_id,
        "enabled": enabled,
        "require_auth": require_auth,
        "require_council_membership": True,
        "require_justification": True,
        "llm_backends": {},
        "members": [],
        "opa": {"enabled": False},
        "council_key_path": "",
        "ledger_path": "",
        "redis": {"host": ""},
        "metadata": metadata or {},
    }


def _write_catalog_and_store(tmp_path, monkeypatch) -> tuple[CouncilCatalogStore, str]:
    catalog_path = tmp_path / "catalog.yaml"
    profile_dir = tmp_path / "councils"
    template_path = tmp_path / "template.yaml"
    alpha_path = profile_dir / "council-alpha.yaml"
    beta_path = profile_dir / "council-beta.yaml"
    disabled_path = profile_dir / "council-disabled.yaml"

    profile_dir.mkdir()
    template_path.write_text(yaml.safe_dump(_profile("template-council")))
    alpha_path.write_text(yaml.safe_dump(_profile("council-alpha")))
    beta_path.write_text(yaml.safe_dump(_profile("council-beta")))
    disabled_path.write_text(yaml.safe_dump(_profile("council-disabled", enabled=False)))
    catalog_path.write_text(
        yaml.safe_dump(
            {
                "default_council_id": "council-alpha",
                "councils": {
                    "council-alpha": {"config_file": "councils/council-alpha.yaml"},
                    "council-beta": {"config_file": "councils/council-beta.yaml"},
                    "council-disabled": {"config_file": "councils/council-disabled.yaml"},
                },
            }
        )
    )

    monkeypatch.setitem(catalog_store.TEMPLATE_CONFIGS, "integration-template", str(template_path))
    monkeypatch.setenv("COUNCIL_DYNAMIC_LEDGER_DIR", str(tmp_path / "ledger"))
    return CouncilCatalogStore(catalog_path=catalog_path, profile_dir=profile_dir), (
        "integration-template"
    )


def _propose(council_id: str = "") -> ProposeIntentInput:
    return ProposeIntentInput(
        auth_token="token",
        agent_id="agent-1",
        council_id=council_id,
        description="Read workspace state before continuing",
        target_resource="/workspace",
        justification="Need to inspect current files before continuing",
    )


def _authorize(council_id: str = "") -> AuthorizeInput:
    return AuthorizeInput(
        auth_token="token",
        agent_id="agent-1",
        council_id=council_id,
        command="/bin/ls /workspace",
        working_directory="/workspace",
        description="Read workspace state before continuing",
        target_resource="/workspace",
        justification="Need to inspect current files before continuing",
    )


@pytest.mark.asyncio
async def test_multi_council_routes_v1_default_v1_explicit_and_v2_explicit(
    tmp_path,
    monkeypatch,
):
    store, _ = _write_catalog_and_store(tmp_path, monkeypatch)
    registry = CouncilRegistry(load_council_catalog(str(store.catalog_path)), store.catalog_path)

    default_service, default_input = await registry.service_for_propose(_propose())
    assert default_service.council_id == "council-alpha"
    assert default_input.council_id == "council-alpha"
    assert default_input.api_version == "v1"

    explicit_v1_service, explicit_v1_input = await registry.service_for_propose(
        _propose("council-beta")
    )
    assert explicit_v1_service.council_id == "council-beta"
    assert explicit_v1_input.council_id == "council-beta"
    assert explicit_v1_input.api_version == "v1"

    explicit_v2_service, explicit_v2_input = await registry.service_for_propose(
        _propose("council-beta"),
        require_explicit=True,
    )
    assert explicit_v2_service.council_id == "council-beta"
    assert explicit_v2_input.council_id == "council-beta"
    assert explicit_v2_input.api_version == "v2"

    authorize_service, authorize_input = await registry.service_for_authorize(
        _authorize("council-beta"),
        require_explicit=True,
    )
    assert authorize_service.council_id == "council-beta"
    assert authorize_input.council_id == "council-beta"
    assert authorize_input.api_version == "v2"


@pytest.mark.asyncio
async def test_multi_council_errors_for_missing_unknown_and_disabled_councils(
    tmp_path,
    monkeypatch,
):
    store, _ = _write_catalog_and_store(tmp_path, monkeypatch)
    registry = CouncilRegistry(load_council_catalog(str(store.catalog_path)), store.catalog_path)

    with pytest.raises(ValueError, match="council_id is required"):
        await registry.service_for_propose(_propose(), require_explicit=True)

    with pytest.raises(ValueError, match="Unknown council_id 'missing-council'"):
        await registry.service_for_propose(_propose("missing-council"), require_explicit=True)

    with pytest.raises(ValueError, match="disabled"):
        await registry.service_for_authorize(
            _authorize("council-disabled"),
            require_explicit=True,
        )


@pytest.mark.asyncio
async def test_dynamic_catalog_hot_reload_create_enable_update_and_disable(
    tmp_path,
    monkeypatch,
):
    store, template = _write_catalog_and_store(tmp_path, monkeypatch)
    registry = CouncilRegistry(load_council_catalog(str(store.catalog_path)), store.catalog_path)

    assert "council-gamma" not in registry.council_ids
    store.create_council(
        "council-gamma",
        template=template,
        description="Generated integration-test council",
        enabled=False,
    )
    with pytest.raises(ValueError, match="disabled"):
        await registry.service_for_propose(_propose("council-gamma"), require_explicit=True)

    store.set_enabled("council-gamma", True)
    service, normalized = await registry.service_for_propose(
        _propose("council-gamma"),
        require_explicit=True,
    )
    assert service.council_id == "council-gamma"
    assert normalized.api_version == "v2"
    assert "council-gamma" in registry.council_ids

    replacement = _profile(
        "ignored-id",
        enabled=True,
        metadata={"owner": "integration-suite"},
    )
    store.update_council_profile("council-gamma", replacement)
    shown = store.show_council("council-gamma")
    assert shown["metadata"]["owner"] == "integration-suite"
    assert shown["metadata"]["source"] == "mcp"

    store.set_enabled("council-gamma", False)
    with pytest.raises(ValueError, match="disabled"):
        await registry.service_for_authorize(_authorize("council-gamma"), require_explicit=True)


def test_dynamic_council_management_contract_lists_shows_validates_and_rejects_bad_ids(
    tmp_path,
    monkeypatch,
):
    store, template = _write_catalog_and_store(tmp_path, monkeypatch)

    listed = store.list_councils()
    assert [item["council_id"] for item in listed] == [
        "council-alpha",
        "council-beta",
        "council-disabled",
    ]
    assert listed[0]["default"] is True

    created = store.create_council(
        "customer-workflow",
        template=template,
        description="Customer workflow council",
        use_case="Review customer-specific automation",
    )
    assert created.enabled is False

    shown = store.show_council("customer-workflow")
    assert shown["council_id"] == "customer-workflow"
    assert shown["metadata"]["description"] == "Customer workflow council"
    assert shown["metadata"]["template"] == template

    validated = store.validate()
    assert "customer-workflow" in validated.councils

    with pytest.raises(CouncilCatalogStoreError, match="council_id"):
        store.create_council("../escape", template=template)

    with pytest.raises(CouncilCatalogStoreError, match="unknown council"):
        store.show_council("unknown-council")


def test_dynamic_council_profile_update_normalizes_id_and_source_metadata(
    tmp_path,
    monkeypatch,
):
    store, _ = _write_catalog_and_store(tmp_path, monkeypatch)
    profile = _profile(
        "../ignored",
        enabled=True,
        metadata={"owner": "platform", "source": "generator"},
    )

    settings = store.update_council_profile(
        "customer-workflow",
        profile,
        enabled=False,
        metadata_updates={"change_id": "CR-123"},
    )

    assert settings.council_id == "customer-workflow"
    assert settings.enabled is False
    assert settings.metadata["owner"] == "platform"
    assert settings.metadata["source"] == "mcp"
    assert settings.metadata["change_id"] == "CR-123"

    shown = store.show_council("customer-workflow")
    assert shown["council_id"] == "customer-workflow"
    assert shown["metadata"]["change_id"] == "CR-123"


@pytest.mark.asyncio
async def test_selected_council_membership_is_enforced_after_routing(tmp_path, monkeypatch):
    store, _ = _write_catalog_and_store(tmp_path, monkeypatch)
    beta_profile = _profile("council-beta", require_auth=True)
    store.update_council_profile("council-beta", beta_profile, enabled=True)
    registry = CouncilRegistry(load_council_catalog(str(store.catalog_path)), store.catalog_path)

    service, request = await registry.service_for_propose(
        _propose("council-beta"),
        require_explicit=True,
    )

    service._authenticator = StubAuthenticator(
        AgentIdentity(
            agent_id="agent-authenticated",
            display_name="Authenticated Agent",
            permissions={"shell:read"},
            councils={"council-alpha"},
        )
    )
    denied = await service.propose_intent(request)
    assert denied.success is False
    assert "not a member of council 'council-beta'" in denied.error

    service._authenticator = StubAuthenticator(
        AgentIdentity(
            agent_id="agent-authenticated",
            display_name="Authenticated Agent",
            permissions={"shell:read"},
            councils={"council-beta"},
        )
    )
    allowed = await service.propose_intent(request)
    assert allowed.success is True
    assert allowed.council_id == "council-beta"
