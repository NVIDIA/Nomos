"""
NAT plugin registration for Nomos council.

This module is loaded by NAT via the entry point:
    [project.entry-points.'nat.plugins']
    nomos = "nomos.nat.plugin"

It registers council functions so they appear in `nat mcp serve`.
"""

import asyncio
import hmac
import logging
import os
from pathlib import Path

from nat.builder.builder import Builder
from nat.cli.register_workflow import register_function
from nat.data_models.function import FunctionBaseConfig
from pydantic import Field

logger = logging.getLogger(__name__)


# ── Config types (the `name` param becomes the `_type` value in YAML) ──


class ProposeIntentConfig(FunctionBaseConfig, name="council_propose_intent"):  # type: ignore[call-arg]
    """NAT config for the propose_intent function."""

    council_config_file: str = Field(
        default="configs/council.yaml",
        description="Path to the Nomos council YAML config",
    )
    workflow_alias: str | None = Field(
        default=None,
        description="Alias when used as workflow entry point",
    )
    enable_debate: bool = Field(
        default=False,
        description="Enable debate rounds when council vote is not unanimous",
    )
    max_debate_rounds: int = Field(
        default=1,
        description="Maximum number of debate rounds (1-3)",
    )


class CheckStatusConfig(FunctionBaseConfig, name="council_check_status"):  # type: ignore[call-arg]
    """NAT config for the check_intent_status function."""

    council_config_file: str = Field(
        default="configs/council.yaml",
        description="Path to the Nomos council YAML config",
    )


class PendingApprovalsConfig(FunctionBaseConfig, name="council_pending_approvals"):  # type: ignore[call-arg]
    """NAT config for the get_pending_approvals function."""

    council_config_file: str = Field(
        default="configs/council.yaml",
        description="Path to the Nomos council YAML config",
    )


class PublicKeyConfig(FunctionBaseConfig, name="council_get_public_key"):  # type: ignore[call-arg]
    """NAT config for the get_council_public_key function."""

    council_config_file: str = Field(
        default="configs/council.yaml",
        description="Path to the Nomos council YAML config",
    )


class LedgerPageConfig(FunctionBaseConfig, name="council_ledger_page"):  # type: ignore[call-arg]
    """NAT config for the council_ledger_page function."""

    council_config_file: str = Field(
        default="configs/council.yaml",
        description="Path to the Nomos council YAML config",
    )


class LedgerEntryConfig(FunctionBaseConfig, name="council_ledger_get"):  # type: ignore[call-arg]
    """NAT config for the council_ledger_get function."""

    council_config_file: str = Field(
        default="configs/council.yaml",
        description="Path to the Nomos council YAML config",
    )


class AuthorizeConfig(FunctionBaseConfig, name="council_authorize"):  # type: ignore[call-arg]
    """NAT config for the council_authorize function."""

    council_config_file: str = Field(
        default="configs/council.yaml",
        description="Path to the Nomos council YAML config",
    )
    enable_debate: bool = Field(
        default=False,
        description="Enable debate rounds when council vote is not unanimous",
    )
    max_debate_rounds: int = Field(
        default=1,
        description="Maximum number of debate rounds (1-3)",
    )


class WritStatusConfig(FunctionBaseConfig, name="council_check_writ_status"):  # type: ignore[call-arg]
    """NAT config for the check_writ_status function."""

    council_config_file: str = Field(
        default="configs/council.yaml",
        description="Path to the Nomos council YAML config",
    )


class CouncilListConfig(FunctionBaseConfig, name="council_list_councils"):  # type: ignore[call-arg]
    """List configured dynamic councils."""

    council_config_file: str = Field(
        default="configs/council.yaml",
        description="Path to the Nomos council catalog YAML config",
    )
    management_token_env: str = Field(
        default="COUNCIL_MANAGEMENT_TOKEN",
        description="Environment variable containing the management token for mutating calls",
    )
    allow_unauthenticated_read: bool = Field(
        default=True,
        description="Allow list/show/validate without a management token",
    )


class CouncilShowConfig(FunctionBaseConfig, name="council_show_council"):  # type: ignore[call-arg]
    """Show one configured dynamic council."""

    council_config_file: str = Field(
        default="configs/council.yaml",
        description="Path to the Nomos council catalog YAML config",
    )
    management_token_env: str = Field(
        default="COUNCIL_MANAGEMENT_TOKEN",
        description="Environment variable containing the management token for mutating calls",
    )
    allow_unauthenticated_read: bool = Field(
        default=True,
        description="Allow list/show/validate without a management token",
    )


class CouncilCreateConfig(FunctionBaseConfig, name="council_create_council"):  # type: ignore[call-arg]
    """Create a dynamic council from a server-side template."""

    council_config_file: str = Field(
        default="configs/council.yaml",
        description="Path to the Nomos council catalog YAML config",
    )
    management_token_env: str = Field(
        default="COUNCIL_MANAGEMENT_TOKEN",
        description="Environment variable containing the management token for mutating calls",
    )
    allow_unauthenticated_read: bool = Field(
        default=True,
        description="Allow list/show/validate without a management token",
    )


class CouncilPutProfileConfig(FunctionBaseConfig, name="council_put_council_profile"):  # type: ignore[call-arg]
    """Create or replace a dynamic council profile from structured MCP input."""

    council_config_file: str = Field(
        default="configs/council.yaml",
        description="Path to the Nomos council catalog YAML config",
    )
    management_token_env: str = Field(
        default="COUNCIL_MANAGEMENT_TOKEN",
        description="Environment variable containing the management token for mutating calls",
    )
    allow_unauthenticated_read: bool = Field(
        default=True,
        description="Allow list/show/validate without a management token",
    )


class CouncilSetEnabledConfig(FunctionBaseConfig, name="council_set_council_enabled"):  # type: ignore[call-arg]
    """Enable or disable a dynamic council profile."""

    council_config_file: str = Field(
        default="configs/council.yaml",
        description="Path to the Nomos council catalog YAML config",
    )
    management_token_env: str = Field(
        default="COUNCIL_MANAGEMENT_TOKEN",
        description="Environment variable containing the management token for mutating calls",
    )
    allow_unauthenticated_read: bool = Field(
        default=True,
        description="Allow list/show/validate without a management token",
    )


class CouncilValidateConfig(FunctionBaseConfig, name="council_validate_catalog"):  # type: ignore[call-arg]
    """Validate the dynamic council catalog."""

    council_config_file: str = Field(
        default="configs/council.yaml",
        description="Path to the Nomos council catalog YAML config",
    )
    management_token_env: str = Field(
        default="COUNCIL_MANAGEMENT_TOKEN",
        description="Environment variable containing the management token for mutating calls",
    )
    allow_unauthenticated_read: bool = Field(
        default=True,
        description="Allow list/show/validate without a management token",
    )


# ── Shared registry cache ──────────────────────────────────────────────

_registry_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    """Return (creating if necessary) the singleton init lock."""
    global _registry_lock
    if _registry_lock is None:
        _registry_lock = asyncio.Lock()
    return _registry_lock


async def _get_registry(config_file: str):
    """Lazy-init the CouncilRegistry for a config file."""
    from nomos.nat.registry import get_registry

    async with _get_lock():
        return await get_registry(config_file)


def _store_for_config(config_file: str):
    """Build a file-backed dynamic council store for this NAT config."""
    from nomos.config.council_catalog_store import CouncilCatalogStore
    from nomos.config.settings import _resolve_env_reference

    catalog_path = Path(str(_resolve_env_reference(config_file, default=config_file))).resolve()
    profile_dir = Path(
        os.environ.get("COUNCIL_DYNAMIC_PROFILE_DIR")
        or os.environ.get("NOMOS_COUNCIL_PROFILE_DIR")
        or str(catalog_path.parent / "councils")
    )
    return CouncilCatalogStore(catalog_path=catalog_path, profile_dir=profile_dir)


def _management_token_error(
    config: object,
    provided_token: str,
    *,
    mutating: bool,
) -> str:
    """Return an error when a management call is not authorized."""
    if mutating and os.environ.get("COUNCIL_STORAGE_MODE", "redis").strip().lower() == "postgres":
        # Profile mutations write replica-local YAML files; with multiple
        # council replicas they would silently diverge. Until a shared
        # (PostgreSQL-backed) catalog store lands, postgres mode supports the
        # static read-only catalog only.
        return (
            "dynamic council management is disabled in postgres storage mode: "
            "profile mutations write replica-local files and would diverge "
            "across council replicas; manage the static catalog instead"
        )
    allow_unauthenticated_read = bool(getattr(config, "allow_unauthenticated_read", True))
    management_token_env = str(getattr(config, "management_token_env", "COUNCIL_MANAGEMENT_TOKEN"))

    if not mutating and allow_unauthenticated_read:
        return ""

    expected = os.environ.get(management_token_env) or os.environ.get(
        "NOMOS_COUNCIL_MANAGEMENT_TOKEN"
    )
    if not expected:
        return (
            f"Council management token is not configured; set {management_token_env} "
            "or NOMOS_COUNCIL_MANAGEMENT_TOKEN"
        )
    if not provided_token or not hmac.compare_digest(provided_token, expected):
        return "Invalid or missing council management token"
    return ""


def _management_error(error: str, *, council_id: str = "") -> str:
    from nomos.nat.functions import CouncilManagementOutput

    return CouncilManagementOutput(
        success=False,
        council_id=council_id,
        error=error,
    ).model_dump_json()


class ProposeIntentV1Config(ProposeIntentConfig, name="council_v1_propose_intent"):  # type: ignore[call-arg]
    """Explicit v1 alias for council_propose_intent."""


class ProposeIntentV2Config(ProposeIntentConfig, name="council_v2_propose_intent"):  # type: ignore[call-arg]
    """v2 propose_intent function; requires council_id."""


class AuthorizeV1Config(AuthorizeConfig, name="council_v1_authorize"):  # type: ignore[call-arg]
    """Explicit v1 alias for council_authorize."""


class AuthorizeV2Config(AuthorizeConfig, name="council_v2_authorize"):  # type: ignore[call-arg]
    """v2 authorize function; requires council_id."""


# ── NAT function registrations ─────────────────────────────────────────


@register_function(config_type=ProposeIntentConfig)
async def council_propose_intent(config: ProposeIntentConfig, builder: Builder):
    from nomos.nat.functions import ProposeIntentInput

    registry = await _get_registry(config.council_config_file)

    async def _propose(input_data: ProposeIntentInput) -> str:
        """Propose an intent for council evaluation. Council members review and vote."""
        service, input_data = await registry.service_for_propose(input_data)
        result = await service.propose_intent(
            input_data,
            enable_debate=config.enable_debate,
            max_debate_rounds=config.max_debate_rounds,
        )
        return result.model_dump_json()  # type: ignore[no-any-return]

    yield _propose


@register_function(config_type=ProposeIntentV1Config)
async def council_v1_propose_intent(config: ProposeIntentV1Config, builder: Builder):
    from nomos.nat.functions import ProposeIntentInput

    registry = await _get_registry(config.council_config_file)

    async def _propose(input_data: ProposeIntentInput) -> str:
        """Explicit v1 alias for proposing an intent."""
        service, input_data = await registry.service_for_propose(input_data)
        result = await service.propose_intent(
            input_data,
            enable_debate=config.enable_debate,
            max_debate_rounds=config.max_debate_rounds,
        )
        return result.model_dump_json()  # type: ignore[no-any-return]

    yield _propose


@register_function(config_type=ProposeIntentV2Config)
async def council_v2_propose_intent(config: ProposeIntentV2Config, builder: Builder):
    from nomos.nat.functions import API_VERSION_V2, ProposeIntentInput, ProposeIntentOutput

    registry = await _get_registry(config.council_config_file)

    async def _propose(input_data: ProposeIntentInput) -> str:
        """Propose an intent to an explicitly selected v2 council."""
        try:
            service, input_data = await registry.service_for_propose(
                input_data,
                require_explicit=True,
            )
        except ValueError as exc:
            return ProposeIntentOutput(
                api_version=API_VERSION_V2,
                council_id=input_data.council_id,
                success=False,
                error=str(exc),
            ).model_dump_json()
        result = await service.propose_intent(
            input_data,
            enable_debate=config.enable_debate,
            max_debate_rounds=config.max_debate_rounds,
        )
        return result.model_dump_json()  # type: ignore[no-any-return]

    yield _propose


@register_function(config_type=CheckStatusConfig)
async def council_check_status(config: CheckStatusConfig, builder: Builder):
    from nomos.nat.functions import IntentStatusInput

    registry = await _get_registry(config.council_config_file)

    async def _check(input_data: IntentStatusInput) -> str:
        """Check the current status of a previously submitted intent."""
        service = await registry.get(input_data.council_id)
        result = service.check_intent_status(input_data)
        return result.model_dump_json()  # type: ignore[no-any-return]

    yield _check


@register_function(config_type=PendingApprovalsConfig)
async def council_pending_approvals(config: PendingApprovalsConfig, builder: Builder):
    from nomos.nat.functions import PendingApprovalsInput, PendingApprovalsOutput

    registry = await _get_registry(config.council_config_file)

    async def _pending(input_data: PendingApprovalsInput) -> str:
        """List all intents currently awaiting council approval."""
        try:
            service = await registry.get(input_data.council_id)
        except ValueError as exc:
            return PendingApprovalsOutput(
                council_id=input_data.council_id,
                error=str(exc),
            ).model_dump_json()
        result = service.get_pending_approvals()
        return result.model_dump_json()  # type: ignore[no-any-return]

    yield _pending


@register_function(config_type=PublicKeyConfig)
async def council_get_public_key(config: PublicKeyConfig, builder: Builder):
    from nomos.nat.functions import PublicKeyInput, PublicKeyOutput

    registry = await _get_registry(config.council_config_file)

    async def _get_key(input_data: PublicKeyInput) -> str:
        """Retrieve the council's Ed25519 public key for writ verification."""
        try:
            service = await registry.get(input_data.council_id)
        except ValueError as exc:
            return PublicKeyOutput(
                council_id=input_data.council_id,
                error=str(exc),
            ).model_dump_json()
        result = service.get_public_key()
        return result.model_dump_json()  # type: ignore[no-any-return]

    yield _get_key


@register_function(config_type=LedgerPageConfig)
async def council_ledger_page(config: LedgerPageConfig, builder: Builder):
    from nomos.nat.functions import LedgerPageInput, LedgerPageOutput

    registry = await _get_registry(config.council_config_file)

    async def _ledger_page(input_data: LedgerPageInput) -> str:
        """Read a page of signed ledger entries (newest first) for audit/monitoring."""
        try:
            service = await registry.get(input_data.council_id)
        except ValueError as exc:
            return LedgerPageOutput(
                council_id=input_data.council_id,
                error=str(exc),
            ).model_dump_json()
        return service.get_ledger_page(input_data).model_dump_json()  # type: ignore[no-any-return]

    yield _ledger_page


@register_function(config_type=LedgerEntryConfig)
async def council_ledger_get(config: LedgerEntryConfig, builder: Builder):
    from nomos.nat.functions import LedgerEntryInput, LedgerEntryOutput

    registry = await _get_registry(config.council_config_file)

    async def _ledger_get(input_data: LedgerEntryInput) -> str:
        """Read one signed ledger entry by sequence number."""
        try:
            service = await registry.get(input_data.council_id)
        except ValueError as exc:
            return LedgerEntryOutput(
                council_id=input_data.council_id,
                error=str(exc),
            ).model_dump_json()
        return service.get_ledger_entry(input_data).model_dump_json()  # type: ignore[no-any-return]

    yield _ledger_get


@register_function(config_type=CouncilListConfig)
async def council_list_councils(config: CouncilListConfig, builder: Builder):
    from nomos.config.council_catalog_store import CouncilCatalogStoreError
    from nomos.nat.functions import CouncilListInput, CouncilManagementOutput

    store = _store_for_config(config.council_config_file)

    async def _list(input_data: CouncilListInput) -> str:
        """List dynamic council profiles known to this MCP service."""
        auth_error = _management_token_error(
            config,
            input_data.management_token,
            mutating=False,
        )
        if auth_error:
            return _management_error(auth_error)
        try:
            catalog = store.load_catalog()
            return CouncilManagementOutput(
                success=True,
                default_council_id=catalog.default_council_id,
                councils=store.list_councils(),
            ).model_dump_json()
        except CouncilCatalogStoreError as exc:
            return _management_error(str(exc))

    yield _list


@register_function(config_type=CouncilShowConfig)
async def council_show_council(config: CouncilShowConfig, builder: Builder):
    from nomos.config.council_catalog_store import CouncilCatalogStoreError
    from nomos.nat.functions import CouncilManagementOutput, CouncilShowInput

    store = _store_for_config(config.council_config_file)

    async def _show(input_data: CouncilShowInput) -> str:
        """Show one dynamic council profile known to this MCP service."""
        auth_error = _management_token_error(
            config,
            input_data.management_token,
            mutating=False,
        )
        if auth_error:
            return _management_error(auth_error, council_id=input_data.council_id)
        try:
            catalog = store.load_catalog()
            return CouncilManagementOutput(
                success=True,
                default_council_id=catalog.default_council_id,
                council_id=input_data.council_id,
                council=store.show_council(input_data.council_id),
            ).model_dump_json()
        except CouncilCatalogStoreError as exc:
            return _management_error(str(exc), council_id=input_data.council_id)

    yield _show


@register_function(config_type=CouncilCreateConfig)
async def council_create_council(config: CouncilCreateConfig, builder: Builder):
    from nomos.config.council_catalog_store import CouncilCatalogStoreError
    from nomos.nat.functions import CouncilCreateInput, CouncilManagementOutput

    store = _store_for_config(config.council_config_file)

    async def _create(input_data: CouncilCreateInput) -> str:
        """Create a dynamic council profile from a server-side template."""
        auth_error = _management_token_error(
            config,
            input_data.management_token,
            mutating=True,
        )
        if auth_error:
            return _management_error(auth_error, council_id=input_data.council_id)
        try:
            store.create_council(
                input_data.council_id,
                template=input_data.template,
                description=input_data.description,
                use_case=input_data.use_case,
                enabled=input_data.enabled,
                force=input_data.force,
            )
            return CouncilManagementOutput(
                success=True,
                council_id=input_data.council_id,
                council=store.show_council(input_data.council_id),
            ).model_dump_json()
        except CouncilCatalogStoreError as exc:
            return _management_error(str(exc), council_id=input_data.council_id)

    yield _create


@register_function(config_type=CouncilPutProfileConfig)
async def council_put_council_profile(config: CouncilPutProfileConfig, builder: Builder):
    from nomos.config.council_catalog_store import CouncilCatalogStoreError
    from nomos.nat.functions import CouncilManagementOutput, CouncilPutProfileInput

    store = _store_for_config(config.council_config_file)

    async def _put(input_data: CouncilPutProfileInput) -> str:
        """Create or replace a dynamic council profile from structured MCP input."""
        auth_error = _management_token_error(
            config,
            input_data.management_token,
            mutating=True,
        )
        if auth_error:
            return _management_error(auth_error, council_id=input_data.council_id)
        try:
            store.update_council_profile(
                input_data.council_id,
                input_data.profile,
                enabled=input_data.enabled,
                metadata_updates={"source": "mcp"},
            )
            return CouncilManagementOutput(
                success=True,
                council_id=input_data.council_id,
                council=store.show_council(input_data.council_id),
            ).model_dump_json()
        except (CouncilCatalogStoreError, ValueError) as exc:
            return _management_error(str(exc), council_id=input_data.council_id)

    yield _put


@register_function(config_type=CouncilSetEnabledConfig)
async def council_set_council_enabled(config: CouncilSetEnabledConfig, builder: Builder):
    from nomos.config.council_catalog_store import CouncilCatalogStoreError
    from nomos.nat.functions import CouncilManagementOutput, CouncilSetEnabledInput

    store = _store_for_config(config.council_config_file)

    async def _set_enabled(input_data: CouncilSetEnabledInput) -> str:
        """Enable or disable a dynamic council profile."""
        auth_error = _management_token_error(
            config,
            input_data.management_token,
            mutating=True,
        )
        if auth_error:
            return _management_error(auth_error, council_id=input_data.council_id)
        try:
            store.set_enabled(input_data.council_id, input_data.enabled)
            return CouncilManagementOutput(
                success=True,
                council_id=input_data.council_id,
                council=store.show_council(input_data.council_id),
            ).model_dump_json()
        except CouncilCatalogStoreError as exc:
            return _management_error(str(exc), council_id=input_data.council_id)

    yield _set_enabled


@register_function(config_type=CouncilValidateConfig)
async def council_validate_catalog(config: CouncilValidateConfig, builder: Builder):
    from nomos.config.council_catalog_store import CouncilCatalogStoreError
    from nomos.nat.functions import CouncilCatalogValidateInput, CouncilManagementOutput

    store = _store_for_config(config.council_config_file)

    async def _validate(input_data: CouncilCatalogValidateInput) -> str:
        """Validate the dynamic council catalog and referenced profiles."""
        auth_error = _management_token_error(
            config,
            input_data.management_token,
            mutating=False,
        )
        if auth_error:
            return _management_error(auth_error)
        try:
            catalog = store.validate()
            return CouncilManagementOutput(
                success=True,
                default_council_id=catalog.default_council_id,
                councils=store.list_councils(),
            ).model_dump_json()
        except (CouncilCatalogStoreError, ValueError) as exc:
            return _management_error(str(exc))

    yield _validate


@register_function(config_type=AuthorizeConfig)
async def council_authorize(config: AuthorizeConfig, builder: Builder):
    from nomos.nat.functions import AuthorizeInput

    registry = await _get_registry(config.council_config_file)

    async def _authorize(input_data: AuthorizeInput) -> str:
        """Authenticate, evaluate, and mint a Writ of Execution if approved."""
        service, input_data = await registry.service_for_authorize(input_data)
        result = await service.authorize(
            input_data,
            enable_debate=config.enable_debate,
            max_debate_rounds=config.max_debate_rounds,
        )
        return result.model_dump_json()  # type: ignore[no-any-return]

    yield _authorize


@register_function(config_type=AuthorizeV1Config)
async def council_v1_authorize(config: AuthorizeV1Config, builder: Builder):
    from nomos.nat.functions import AuthorizeInput

    registry = await _get_registry(config.council_config_file)

    async def _authorize(input_data: AuthorizeInput) -> str:
        """Explicit v1 alias for council authorization."""
        service, input_data = await registry.service_for_authorize(input_data)
        result = await service.authorize(
            input_data,
            enable_debate=config.enable_debate,
            max_debate_rounds=config.max_debate_rounds,
        )
        return result.model_dump_json()  # type: ignore[no-any-return]

    yield _authorize


@register_function(config_type=AuthorizeV2Config)
async def council_v2_authorize(config: AuthorizeV2Config, builder: Builder):
    from nomos.nat.functions import API_VERSION_V2, AuthorizeInput, AuthorizeOutput

    registry = await _get_registry(config.council_config_file)

    async def _authorize(input_data: AuthorizeInput) -> str:
        """Authorize through an explicitly selected v2 council."""
        try:
            service, input_data = await registry.service_for_authorize(
                input_data,
                require_explicit=True,
            )
        except ValueError as exc:
            return AuthorizeOutput(
                api_version=API_VERSION_V2,
                council_id=input_data.council_id,
                status="error",
                error=str(exc),
            ).model_dump_json()
        result = await service.authorize(
            input_data,
            enable_debate=config.enable_debate,
            max_debate_rounds=config.max_debate_rounds,
        )
        return result.model_dump_json()  # type: ignore[no-any-return]

    yield _authorize


@register_function(config_type=WritStatusConfig)
async def council_check_writ_status(config: WritStatusConfig, builder: Builder):
    from nomos.nat.functions import WritStatusInput

    registry = await _get_registry(config.council_config_file)

    async def _check_writ(input_data: WritStatusInput) -> str:
        """Check the status of a previously issued Writ of Execution."""
        service = await registry.get(input_data.council_id)
        result = await service.check_writ_status(input_data)
        return result.model_dump_json()  # type: ignore[no-any-return]

    yield _check_writ
