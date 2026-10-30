# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Typed plugin settings and the configuration JSON Schema they come from.

``SCHEMA`` is the single source of the shipped ``config.schema.json``. Every
leaf also accepts a value reference (see ``values``); references are resolved,
then coerced to the leaf's type, then the result is validated again.
"""

from __future__ import annotations

import fnmatch
import math
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, NamedTuple
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

from .values import ConfigReferenceError, resolve_tree

RESERVED_PREFIX = "nomos_"
LOOPBACK = ("127.0.0.1", "::1", "localhost")
SECRET_FIELDS = (("connection", "auth_token"), ("connection", "proxy_token"), (None, "signing_key"))
EXECUTORS = ("nomos_http", "local")
CLAIMS = ("auto", "executor", "local_ledger", "council")
COUNCIL_KEY_SOURCES = ("pinned", "council")
TOOL_DEFINITIONS = ("off", "called", "all")
HEX64 = "^[0-9a-f]{64}$"
_REFERENCE = {"$ref": "#/$defs/reference"}


def _leaf(schema: dict, description: str, **extra: Any) -> dict:
    return {"description": description, **extra, "anyOf": [schema, _REFERENCE]}


def _object(description: str, properties: dict, required: tuple[str, ...] = (), **extra: Any) -> dict:
    out = {
        "type": "object",
        "description": description,
        "additionalProperties": False,
        "properties": properties,
        **extra,
    }
    if required:
        out["required"] = list(required)
    return out


def _string(
    description: str, default: str | None = None, *, secret: bool = False, **constraints: Any
) -> dict:
    extra: dict[str, Any] = {} if default is None else {"default": default}
    if secret:
        # Relay (0.10 and 0.11) rejects writeOnly under anyOf, so a reference-capable field cannot be masked by the
        # Relay editor. Supply secrets as {"file": ...} references so plugins.toml never holds them.
        description += " Secret: supply it as a file reference rather than inline."
    return _leaf({"type": "string", **constraints}, description, **extra)


def _number(description: str, default: float | None, kind: str = "number", **constraints: Any) -> dict:
    return _leaf(
        {"type": kind, **constraints}, description, **({} if default is None else {"default": default})
    )


def _ref_shape(description: str, keys: dict[str, dict]) -> dict:
    return {
        "type": "object",
        "description": description,
        "additionalProperties": False,
        "required": list(keys),
        "properties": keys,
    }


_NAME = {"type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_]{0,127}$"}
_ABS = {"type": "string", "pattern": "^/"}
_PATTERNS = {"type": "array", "items": {"anyOf": [{"type": "string", "minLength": 1}, _REFERENCE]}}

LIMIT_DEFAULTS = {
    "max_result_bytes": 2_097_152,
    "max_storage_bytes": 67_108_864,
    "max_records": 1000,
    "max_invocations": 10000,
    "retention_seconds": 3600,
    "max_citation_bytes": 4_194_304,
    "max_output_chars": 65536,
    "conversation_share_percent": 25,
}
_LIMIT_BOUNDS = {
    "max_result_bytes": (1024, 16_777_216),
    "max_storage_bytes": (65536, 1_073_741_824),
    "max_records": (1, 100_000),
    "max_invocations": (1, 1_000_000),
    "retention_seconds": (60, 31_536_000),
    "max_citation_bytes": (1024, 16_777_216),
    "max_output_chars": (256, 1_048_576),
    "conversation_share_percent": (1, 100),
}

SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$comment": (
        "SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved. "
        "SPDX-License-Identifier: Apache-2.0. Generated from nemo_relay_nomos.settings.SCHEMA; do not edit."
    ),
    "title": "nomos.council plugin configuration",
    "type": "object",
    "additionalProperties": False,
    "required": ["connection", "trust"],
    "$defs": {
        "reference": {
            "description": "A value read at startup instead of written inline. Errors never show the value.",
            "oneOf": [
                _ref_shape("An inline value.", {"value": {}}),
                _ref_shape("An environment variable of the worker process.", {"env": _NAME}),
                _ref_shape(
                    "A UTF-8 file owned by the worker user with mode 0600 or 0400 (one trailing newline is "
                    "removed).",
                    {"file": _ABS},
                ),
                _ref_shape(
                    "Encrypted value; not supported yet (TODO).",
                    {"encrypted": {"type": "string"}, "key_env": _NAME},
                ),
                _ref_shape(
                    "Encrypted value; not supported yet (TODO).",
                    {"encrypted": {"type": "string"}, "key_file": _ABS},
                ),
            ],
        }
    },
    "properties": {
        "connection": _object(
            "How to reach the Nomos Council and trusted Executor, and who this agent is.",
            {
                "mcp_url": _string("Council MCP endpoint (streamable HTTP).", "http://127.0.0.1:9901/mcp"),
                "executor_url": _string("Trusted Executor base URL.", "http://127.0.0.1:9907"),
                "agent_id": _string("Nomos agent account id.", minLength=1),
                "council_id": _string("Council that authorizes this agent's calls.", minLength=1),
                "auth_token": _string("Nomos agent token, sent in council_authorize.", "", secret=True),
                "proxy_token": _string(
                    "Bearer token for the Monitor MCP proxy, if it requires one.", "", secret=True
                ),
                "working_directory": _string(
                    "Working directory authorized for commands.", "/workspace", minLength=1
                ),
                "timeout_s": _number(
                    "Timeout for each Council or Executor request.", 180.0, exclusiveMinimum=0
                ),
                "allow_insecure_http": _leaf(
                    {"type": "boolean"},
                    "Allow http:// to a non-loopback Council or Executor (tokens and evidence travel in clear).",
                    default=False,
                ),
            },
            ("agent_id", "council_id"),
            default={},
        ),
        "guard": _object(
            "Which tools Nomos governs. A tool is guarded when it matches an include pattern and no "
            "exclude pattern. Use include ['*'] for all tools, a list for an allow-list, or ['*'] plus "
            "exclude for a deny-list. Names starting with 'nomos_' are never guarded.",
            {
                "include": {
                    **_PATTERNS,
                    "description": "Tool names or shell-style patterns.",
                    "default": ["run_shell"],
                },
                "exclude": {
                    **_PATTERNS,
                    "description": "Tool names or patterns never guarded.",
                    "default": [],
                },
            },
            default={"include": ["run_shell"], "exclude": []},
        ),
        "tools": {
            "type": "object",
            "description": "Per-tool execution policy. Unlisted guarded tools use default_executor.",
            "additionalProperties": _object(
                "Execution policy for one tool.",
                {
                    "executor": _leaf(
                        {"enum": list(EXECUTORS)},
                        "nomos_http runs the approved command on the trusted Executor; local runs the "
                        "application's own tool function after approval.",
                        default="local",
                    ),
                    "command_argument": _string(
                        "For nomos_http: the single argument holding the shell command.", minLength=1
                    ),
                },
            ),
            "default": {"run_shell": {"executor": "nomos_http", "command_argument": "command"}},
        },
        "tool_call": _object(
            "Settings for structured local-tool authorization.",
            {
                "subject": _string(
                    "Opaque host-derived subject bound into structured tool-call writs. Use only when "
                    "one plugin activation serves one subject; never supply model-generated identity.",
                    minLength=1,
                    maxLength=512,
                    pattern=r"^[^\x00-\x1f\x7f\u2028\u2029]+$",
                ),
            },
        ),
        "default_executor": _leaf(
            {"enum": list(EXECUTORS)}, "Executor for guarded tools not listed in tools.", default="local"
        ),
        "writ_handler": _object(
            "What happens to an approved writ.",
            {
                "kind": _leaf(
                    {"enum": ["default", "custom"]},
                    "default validates, claims and dispatches the writ. custom is reserved (not implemented).",
                    default="default",
                ),
                "claim": _leaf(
                    {"enum": list(CLAIMS)},
                    "How single use is claimed. auto: the Executor claims nomos_http writs, the local ledger "
                    "claims local ones. council is reserved for a Nomos claim API.",
                    default="auto",
                ),
                "entrypoint": _string("custom only: 'package.module:attribute' of a WritHandler (reserved)."),
                "options": {
                    "type": "object",
                    "description": "custom only: options passed to the handler (reserved).",
                    "default": {},
                },
            },
            default={"kind": "default", "claim": "auto"},
        ),
        "max_denials": _number(
            "Consecutive Council denials before the plugin stops asking.",
            3,
            "integer",
            minimum=1,
            maximum=100,
        ),
        "catalog": _object(
            "The evidence catalog appended to each model request.",
            {
                "enabled": _leaf({"type": "boolean"}, "Append the catalog.", default=True),
                "max_entries": _number("Most recent entries listed.", 20, "integer", minimum=1, maximum=200),
                "max_argument_chars": _number(
                    "Characters of JSON arguments shown per entry.", 400, "integer", minimum=32, maximum=8192
                ),
            },
            default={"enabled": True, "max_entries": 20, "max_argument_chars": 400},
        ),
        "tool_definitions": _leaf(
            {"enum": list(TOOL_DEFINITIONS)},
            "Send the Council the tool definitions the model was offered, as H1 saw them in the model request, "
            "as one certified evidence item: a Nomos ExecutionResult signed with signing_key, which the Council "
            "certifies once the operator registers that key as a trusted executor (executor id "
            "nomos-relay-plugin:<first 16 hex of the key fingerprint>). off: none. called: the called tool's "
            "definition and a digest of the whole list. all: every definition (the Council shows the first "
            "4000 characters). When on, the model may cite one item fewer (19). Needs signing_key; ignored "
            "without one.",
            default="off",
        ),
        "session": _object(
            "This activation's evidence store. It is held in the worker's memory and lasts as long as the "
            "worker; the conversations it serves are told apart by the turn tokens in their own transcripts.",
            {
                "id": _string(
                    "Operator-assigned session label. Omit to use a random id per activation.", minLength=1
                ),
            },
        ),
        "signing_key": _string(
            "Plugin Ed25519 seed, 64 hex characters, which signs every evidence record and certifies the local "
            "results and Council decisions it keeps (and, with tool_definitions on, the tool definitions): register "
            "it with the Council as a trusted executor to cite them. Omit to keep no evidence: no record or tool "
            "list is stored, and model requests offer no catalog or evidence field.",
            secret=True,
            pattern=HEX64,
        ),
        "trust": _object(
            "Operator-provisioned trust anchors. Executor keys are always pinned here; the Council's key is "
            "pinned too unless council_public_key_source is 'council'.",
            {
                "policy_version": _string("Label of this trust snapshot.", minLength=1),
                "valid_until": _number("Unix time after which nothing is trusted.", None, exclusiveMinimum=0),
                "council_public_key_hex": _string(
                    "Council Ed25519 public key (raw, hex). Required when council_public_key_source is 'pinned'.",
                    pattern=HEX64,
                ),
                "council_public_key_source": _leaf(
                    {"enum": list(COUNCIL_KEY_SOURCES)},
                    "pinned: council_public_key_hex verifies writs. council: the key is fetched from "
                    "connection.mcp_url (council_get_public_key) on the first guarded call and trusted for the "
                    "rest of this activation; whoever controls that endpoint then controls approvals.",
                    default="pinned",
                ),
                "executors": {
                    "type": "object",
                    "description": "Executor id to its trusted public keys (raw, hex); several allow rotation.",
                    "minProperties": 1,
                    "additionalProperties": {
                        "type": "array",
                        "minItems": 1,
                        "items": _string("Executor public key.", pattern=HEX64),
                    },
                },
            },
            ("policy_version", "valid_until", "executors"),
        ),
        "limits": _object(
            "Resource limits.",
            {
                key: _number(
                    key.replace("_", " ").capitalize() + ".", value, "integer", minimum=lo, maximum=hi
                )
                for key, value in LIMIT_DEFAULTS.items()
                for lo, hi in [_LIMIT_BOUNDS[key]]
            },
            default=dict(LIMIT_DEFAULTS),
        ),
    },
}


class Diagnostic(NamedTuple):
    field: str
    message: str


class SettingsError(ValueError):
    def __init__(self, diagnostics: list[Diagnostic]):
        super().__init__("; ".join(f"{d.field}: {d.message}" for d in diagnostics))
        self.diagnostics = diagnostics


@dataclass(frozen=True)
class Connection:
    agent_id: str
    council_id: str
    mcp_url: str = "http://127.0.0.1:9901/mcp"
    executor_url: str = "http://127.0.0.1:9907"
    auth_token: str = field(default="", repr=False)
    proxy_token: str = field(default="", repr=False)
    working_directory: str = "/workspace"
    timeout_s: float = 180.0
    allow_insecure_http: bool = False


@dataclass(frozen=True)
class Guard:
    include: tuple[str, ...] = ("run_shell",)
    exclude: tuple[str, ...] = ()

    def matches(self, name: str) -> bool:
        if name.startswith(RESERVED_PREFIX):
            return False
        hit = any(fnmatch.fnmatchcase(name, p) for p in self.include)
        return hit and not any(fnmatch.fnmatchcase(name, p) for p in self.exclude)


@dataclass(frozen=True)
class ToolPolicy:
    executor: str = "local"
    command_argument: str | None = None


@dataclass(frozen=True)
class ToolCallSettings:
    """Operator-owned identity used by structured local-tool authorization."""

    subject: str | None = None


@dataclass(frozen=True)
class WritHandlerSettings:
    kind: str = "default"
    claim: str = "auto"
    entrypoint: str | None = None
    options: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CatalogSettings:
    enabled: bool = True
    max_entries: int = 20
    max_argument_chars: int = 400


@dataclass(frozen=True)
class Session:
    id: str


@dataclass(frozen=True)
class Trust:
    policy_version: str
    valid_until: float
    executors: dict
    # None until fetched when the source is "council" (see NomosPlugin._council_key).
    council_public_key_hex: str | None = None
    council_public_key_source: str = "pinned"

    def as_dict(self) -> dict:
        return {
            "policy_version": self.policy_version,
            "valid_until": self.valid_until,
            "council_public_key_hex": self.council_public_key_hex,
            "executors": self.executors,
        }


@dataclass(frozen=True)
class Limits:
    max_result_bytes: int = LIMIT_DEFAULTS["max_result_bytes"]
    max_storage_bytes: int = LIMIT_DEFAULTS["max_storage_bytes"]
    max_records: int = LIMIT_DEFAULTS["max_records"]
    max_invocations: int = LIMIT_DEFAULTS["max_invocations"]
    retention_seconds: int = LIMIT_DEFAULTS["retention_seconds"]
    max_citation_bytes: int = LIMIT_DEFAULTS["max_citation_bytes"]
    max_output_chars: int = LIMIT_DEFAULTS["max_output_chars"]
    conversation_share_percent: int = LIMIT_DEFAULTS["conversation_share_percent"]
    # Not configured here: true when a signing_key is set, so local results are certified.
    certified_local_results: bool = False

    @property
    def reserve_bytes(self) -> int:
        """Storage held back before dispatch: one result plus its journal and records.

        A certified local result stores the output three times: in the record, in the certified copy's stdout,
        where it is escaped again (up to twice its size), and in the replay journal's outcome.
        """
        result = self.max_result_bytes + self.max_output_chars * 4
        return result * (4 if self.certified_local_results else 1) + 65536 + 8192


@dataclass(frozen=True)
class Settings:
    connection: Connection
    session: Session
    trust: Trust
    guard: Guard = Guard()
    tools: dict = field(default_factory=lambda: {"run_shell": ToolPolicy("nomos_http", "command")})
    default_executor: str = "local"
    tool_call: ToolCallSettings = ToolCallSettings()
    writ_handler: WritHandlerSettings = WritHandlerSettings()
    max_denials: int = 3
    catalog: CatalogSettings = CatalogSettings()
    tool_definitions: str = "off"
    signing_key: str | None = field(default=None, repr=False)
    limits: Limits = Limits()

    def policy(self, name: str) -> ToolPolicy:
        return self.tools.get(name) or ToolPolicy(self.default_executor, None)

    def claim_for(self, policy: ToolPolicy) -> str:
        if self.writ_handler.claim != "auto":
            return self.writ_handler.claim
        return "executor" if policy.executor == "nomos_http" else "local_ledger"

    @classmethod
    def from_config(cls, raw: dict, *, environ: Mapping[str, str] | None = None) -> Settings:
        problems, resolved = _check(raw, environ)
        if problems:
            raise SettingsError(problems)
        return _build(resolved)


def diagnostics(raw: Any, *, environ: Mapping[str, str] | None = None) -> list[Diagnostic]:
    return _check(raw, environ)[0]


def warnings(raw: Any) -> list[Diagnostic]:
    """Non-blocking advice: secrets written inline in plugins.toml rather than as file references."""
    out = []
    for section, key in SECRET_FIELDS:
        holder = raw.get(section, {}) if section else raw
        value = holder.get(key) if isinstance(holder, dict) else None
        if isinstance(value, str) and value:
            name = f"{section}.{key}" if section else key
            out.append(Diagnostic(name, "a secret is written inline in plugins.toml; use a file reference"))
    return out


def _schema_errors(value: Any) -> list[Diagnostic]:
    out = []
    for e in sorted(Draft202012Validator(SCHEMA).iter_errors(value), key=lambda e: list(map(str, e.path))):
        # An anyOf(leaf, reference) failure is clearest as the leaf's own error.
        leaf = e.context[0] if e.validator == "anyOf" and e.context else e
        out.append(Diagnostic(".".join(map(str, e.absolute_path)) or "<root>", _describe(leaf)))
    return out


def _describe(error: Any) -> str:
    """A schema error that never quotes the offending value (it may be a resolved secret)."""
    rule, expected = error.validator, error.validator_value
    if rule in ("required", "additionalProperties"):
        return error.message  # names keys, never values
    if rule == "type":
        return f"must be of type {expected}"
    if rule == "pattern":
        return f"must match the pattern {expected}"
    if rule == "enum":
        return f"must be one of {expected}"
    if rule in (
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "minProperties",
        "uniqueItems",
        "const",
    ):
        return f"violates {rule} {expected}"
    return f"does not satisfy {rule}"


def _check(raw: Any, environ: Mapping[str, str] | None) -> tuple[list[Diagnostic], dict]:
    errors = _schema_errors(raw)
    if errors:
        return errors, {}
    try:
        resolved = _coerce(resolve_tree(raw, environ=environ), SCHEMA)
    except ConfigReferenceError as exc:
        return [Diagnostic(exc.field, exc.message)], {}
    errors = _schema_errors(resolved)
    return (errors or _semantic(resolved)), resolved


def _plain_type(schema: dict) -> str | None:
    for option in schema.get("anyOf", [schema]):
        if option.get("type") in ("number", "integer", "boolean"):
            return option["type"]
    return None


def _coerce(value: Any, schema: dict) -> Any:
    """Turn reference-supplied strings into the leaf's declared type."""
    if isinstance(value, dict) and schema.get("type") == "object":
        props, extra = schema.get("properties", {}), schema.get("additionalProperties")
        return {
            k: _coerce(v, props.get(k) or (extra if isinstance(extra, dict) else {}))
            for k, v in value.items()
        }
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        return [_coerce(v, schema["items"]) for v in value]
    kind = _plain_type(schema)
    if isinstance(value, str) and kind:
        text = value.strip()
        try:
            if kind == "boolean":
                return {"true": True, "false": False}[text.lower()]
            number = float(text)
            return int(number) if kind == "integer" and number.is_integer() else number
        except (KeyError, ValueError):
            return value  # left for the second schema pass to report
    return value


def _semantic(cfg: dict) -> list[Diagnostic]:
    out: list[Diagnostic] = []
    tools = cfg.get("tools", SCHEMA["properties"]["tools"]["default"])
    guard_cfg = cfg.get("guard", {})
    guard = Guard(tuple(guard_cfg.get("include", ["run_shell"])), tuple(guard_cfg.get("exclude", [])))
    # A glob can select future tool names that are absent from the explicit map.
    implicit_local = cfg.get("default_executor", "local") == "local" and any(
        (
            pattern not in guard.exclude
            and "*" not in guard.exclude
            and not pattern.startswith(RESERVED_PREFIX)
            if any(char in pattern for char in "*?[")
            else guard.matches(pattern) and pattern not in tools
        )
        for pattern in guard.include
    )
    needs_subject = implicit_local or any(
        policy.get("executor", "local") == "local" for policy in tools.values()
    )
    if needs_subject and not cfg.get("tool_call", {}).get("subject"):
        out.append(Diagnostic("tool_call.subject", "guarded local tools require an operator-derived subject"))
    for name, policy in tools.items():
        executor = policy.get("executor", "local")
        if executor == "nomos_http" and not policy.get("command_argument"):
            out.append(
                Diagnostic(f"tools.{name}.command_argument", "a nomos_http tool needs command_argument")
            )
        if executor == "local" and policy.get("command_argument"):
            out.append(
                Diagnostic(
                    f"tools.{name}.command_argument",
                    "local tools authorize structured arguments and cannot use command_argument",
                )
            )
        if executor == "local" and cfg.get("writ_handler", {}).get("claim") == "executor":
            out.append(
                Diagnostic(f"tools.{name}.executor", "claim 'executor' cannot claim a local tool's writ")
            )
    handler = cfg.get("writ_handler", {})
    if handler.get("claim") == "executor" and cfg.get("default_executor", "local") == "local":
        out.append(
            Diagnostic(
                "writ_handler.claim", "claim 'executor' cannot claim writs of local tools (default_executor)"
            )
        )
    connection = cfg.get("connection", {})
    for key, default in (("mcp_url", "http://127.0.0.1:9901/mcp"), ("executor_url", "http://127.0.0.1:9907")):
        url = urlsplit(connection.get(key, default))
        if url.scheme not in ("https", "http") or not url.hostname:
            out.append(Diagnostic(f"connection.{key}", "must be an http(s) URL with a host"))
        elif (
            url.scheme == "http"
            and url.hostname not in LOOPBACK
            and not connection.get("allow_insecure_http")
        ):
            out.append(
                Diagnostic(
                    f"connection.{key}",
                    "http:// to a non-loopback host sends tokens and evidence in clear; use https or set "
                    "connection.allow_insecure_http",
                )
            )
    trust = cfg["trust"]
    if trust.get("council_public_key_source", "pinned") == "pinned":
        if not trust.get("council_public_key_hex"):
            out.append(
                Diagnostic(
                    "trust.council_public_key_hex",
                    "required unless trust.council_public_key_source is 'council'",
                )
            )
    else:
        if trust.get("council_public_key_hex"):
            out.append(
                Diagnostic(
                    "trust.council_public_key_source",
                    "'council' fetches the Council's key; remove trust.council_public_key_hex, or pin it instead",
                )
            )
        url = urlsplit(connection.get("mcp_url", "http://127.0.0.1:9901/mcp"))
        if url.scheme != "https" and url.hostname not in LOOPBACK:
            out.append(
                Diagnostic(
                    "trust.council_public_key_source",
                    "fetching the Council's key needs an https (or loopback) connection.mcp_url, whatever "
                    "connection.allow_insecure_http says: over http anyone on the path could supply the key",
                )
            )
    if handler.get("kind") == "custom":
        out.append(Diagnostic("writ_handler.kind", "custom writ handlers are not implemented yet"))
    if handler.get("claim") == "council":
        out.append(
            Diagnostic("writ_handler.claim", "the pinned Nomos has no writ claim API (council_consume_writ)")
        )
    valid_until = trust["valid_until"]
    if not math.isfinite(valid_until) or valid_until <= time.time():
        out.append(Diagnostic("trust.valid_until", "trust snapshot has expired"))
    limits = _limits(cfg)
    # One conversation's share must still hold one full result.
    least = -(-limits.reserve_bytes * 100 // limits.conversation_share_percent)
    if limits.max_storage_bytes < least:
        out.append(Diagnostic("limits.max_storage_bytes", f"must be at least {least}"))
    return out


def _build(cfg: dict) -> Settings:
    tools_raw = cfg.get("tools", SCHEMA["properties"]["tools"]["default"])
    guard = cfg.get("guard", {})
    handler = cfg.get("writ_handler", {})
    return Settings(
        connection=Connection(**cfg["connection"]),
        session=Session(cfg.get("session", {}).get("id") or f"activation-{uuid.uuid4().hex[:12]}"),
        trust=Trust(**cfg["trust"]),
        guard=Guard(tuple(guard.get("include", ["run_shell"])), tuple(guard.get("exclude", []))),
        tools={
            n: ToolPolicy(
                p.get("executor", "local"),
                p.get("command_argument"),
            )
            for n, p in tools_raw.items()
        },
        default_executor=cfg.get("default_executor", "local"),
        tool_call=ToolCallSettings(**cfg.get("tool_call", {})),
        writ_handler=WritHandlerSettings(
            handler.get("kind", "default"),
            handler.get("claim", "auto"),
            handler.get("entrypoint"),
            dict(handler.get("options", {})),
        ),
        max_denials=cfg.get("max_denials", 3),
        catalog=CatalogSettings(**cfg.get("catalog", {})),
        tool_definitions=cfg.get("tool_definitions", "off"),
        signing_key=cfg.get("signing_key"),
        limits=_limits(cfg),
    )


def _limits(cfg: dict) -> Limits:
    certified = bool(cfg.get("signing_key"))
    return Limits(**{**LIMIT_DEFAULTS, **cfg.get("limits", {})}, certified_local_results=certified)


def schema_text() -> str:
    import json

    return json.dumps(SCHEMA, indent=2) + "\n"


if __name__ == "__main__":
    print(schema_text(), end="")
