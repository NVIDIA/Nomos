# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""The Relay worker: Nomos oversight for every conversation in one host process.

H1 (LLM request intercept, priority 10) requires an intent on every guarded
tool, issues the request a turn token, and appends the Nomos message (the token
and the evidence catalog). H3 (tool execution intercept, priority 90, before
Relay's caches and guardrails) resolves the call's turn token to its conversation, turns the call
into a Council intent, and either returns the Council's refusal or hands the
writ to the writ handler. Every response H3 produces for a guarded call is
stored as signed evidence.

Relay gives the worker no caller identity, so conversations are told apart by
the turn tokens in their own transcripts; state is never shared between them.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import sqlite3
import time
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from nemo_relay_plugin import (
    ConfigDiagnostic,
    DiagnosticLevel,
    LlmRequestInterceptOutcome,
    ToolExecutionInterceptOutcome,
    WorkerPlugin,
)

from .llm_request import (
    MAX_CITATIONS,
    TEACH_TURN,
    append_message,
    inject_intent,
    intent_problems,
    nomos_field,
    render_message,
    teach_nomos,
    transcript_ids,
)
from .nomos_client import Council, Executor, HttpExecutor, McpCouncil
from .records import catalog_entry, citation, integrity_ok, plugin_record, response
from .repository import EvidenceStore, StoreError, Turn
from .settings import Settings, diagnostics, warnings
from .signing import PluginSigner
from .tool_call import StructuredToolCall
from .verify import (
    EvidenceError,
    check_trust,
    decode_tool_call_writ,
    decode_writ,
    verify_executor_evidence,
    verify_plugin_certified,
)
from .wire import bounded, canonical, sha256_hex
from .writs import WritCall, WritHandler, load_writ_handler

PLUGIN_KIND = "nomos.council"
REQUEST_PRIORITY = 10
# Lower runs first. Relay's tool and LLM caches default to 150 (100 in 0.10) and NeMo Guardrails to 100; a cache hit
# returns before every later execution intercept, so authorization must run before any cache, as Relay prescribes for
# policy checks. The cost: for a local tool, an execution intercept after this one could still change the arguments
# the tool receives (a nomos_http call never reaches later intercepts: the plugin dispatches it to the Executor).
EXECUTION_PRIORITY = 90
MAX_USER_TEXT = 800
_UNKNOWN_STORE_ERRORS = {"execution_unknown", "session_requires_reconciliation", "journal_integrity_failure"}
_ANNOTATED_KEYS = (
    "contract_version",
    "status",
    "evidence_id",
    "quarantine_id",
    "writ_id",
    "execution_status",
    "evidence_status",
)


TOOL_DEFINITIONS_COMMAND = (
    "(no command ran) nomos-relay-plugin observed the tool definitions the application offered the model in the "
    "request this call answers; stdout is that observation"
)
COUNCIL_DECISION_COMMAND = (
    "(no command ran) nomos-relay-plugin observed the Council's answer, which authorized nothing, to a call of the "
    "tool {tool}; stdout is its status, guidance and missing permissions"
)

TRUST_MESSAGES = {
    "trust_policy_expired": "The operator's trust snapshot has expired.",
    "council_key_unavailable": "The Council's public key could not be obtained; nothing was submitted.",
}


def _fetches_council_key(plugin_config: Any) -> bool:
    trust = plugin_config.get("trust") if isinstance(plugin_config, dict) else None
    source = trust.get("council_public_key_source") if isinstance(trust, dict) else None
    if isinstance(source, dict):
        source = source.get("value")
    return source == "council"


class NomosPlugin(WorkerPlugin):
    plugin_id = PLUGIN_KIND
    allows_multiple_components = False

    def __init__(
        self,
        council: Council | None = None,
        executor: Executor | None = None,
        store: EvidenceStore | None = None,
        environ: Mapping[str, str] | None = None,
    ):
        self._council, self._executor, self.store, self.environ = council, executor, store, environ
        self.settings: Settings | None = None
        self.signer: PluginSigner | None = None
        self.handler: WritHandler | None = None
        self._busy: set[str] = set()  # conversations with a guarded call in progress
        self._key_lock = asyncio.Lock()  # one fetch of the Council's key at a time

    # ── lifecycle ────────────────────────────────────────────────────────

    # The SDK declares list[ConfigDiagnostic | dict]; list is invariant, so a narrower list needs the ignore.
    def validate(self, plugin_config: dict) -> list[ConfigDiagnostic]:  # type: ignore[override]
        problems = diagnostics(plugin_config, environ=self.environ)
        # The opt-in warnings read the resolved settings, so a reference ({"value": …}, env, file) counts too.
        s = None if problems else Settings.from_config(plugin_config, environ=self.environ)
        return (
            [
                ConfigDiagnostic(
                    level=DiagnosticLevel.ERROR,
                    code="nomos.config",
                    component=PLUGIN_KIND,
                    field=d.field,
                    message=d.message,
                )
                for d in problems
            ]
            + [
                ConfigDiagnostic(
                    level=DiagnosticLevel.WARNING,
                    code="nomos.config.inline_secret",
                    component=PLUGIN_KIND,
                    field=d.field,
                    message=d.message,
                )
                for d in warnings(plugin_config)
            ]
            + (
                []
                if isinstance(plugin_config, dict) and plugin_config.get("signing_key")
                else [
                    ConfigDiagnostic(
                        level=DiagnosticLevel.WARNING,
                        code="nomos.config.evidence_disabled",
                        component=PLUGIN_KIND,
                        field="signing_key",
                        message="no signing_key: evidence is disabled (no record is stored, nothing can be cited)",
                    )
                ]
            )
            + (
                [
                    ConfigDiagnostic(
                        level=DiagnosticLevel.WARNING,
                        code="nomos.config.council_key_fetched",
                        component=PLUGIN_KIND,
                        field="trust.council_public_key_source",
                        message="the Council's key will be taken from connection.mcp_url, not pinned: whoever "
                        "controls that endpoint (or its TLS) controls approvals",
                    )
                ]
                if _fetches_council_key(plugin_config)
                else []
            )
            + (
                [
                    ConfigDiagnostic(
                        level=DiagnosticLevel.WARNING,
                        code="nomos.config.tool_definitions_disabled",
                        component=PLUGIN_KIND,
                        field="tool_definitions",
                        message="no signing_key: tool_definitions is ignored (the definitions are sent as "
                        "certified evidence, which must be signed)",
                    )
                ]
                if s is not None and s.tool_definitions != "off" and not s.signing_key
                else []
            )
        )

    def register(self, context: Any, plugin_config: dict) -> None:
        problems = diagnostics(plugin_config, environ=self.environ)
        if problems:
            raise ValueError(f"{problems[0].field}: {problems[0].message}")
        s = self.settings = Settings.from_config(plugin_config, environ=self.environ)
        owner = (s.connection.agent_id, s.connection.council_id, s.session.id)
        if self.store is None:
            self.store = EvidenceStore(owner, s.limits)
        elif self.store.owner != owner:
            raise ValueError("injected repository owner does not match the configured session")
        # Evidence must be signed, and the plugin never makes a key up: without a configured key it keeps none.
        self.signer = PluginSigner(bytes.fromhex(s.signing_key)) if s.signing_key else None
        council = self._council or McpCouncil(s.connection)
        self._council = council
        self._executor = self._executor or HttpExecutor(s.connection, s.limits.max_result_bytes)
        self.handler = load_writ_handler(s, store=self.store, executor=self._executor, signer=self.signer)
        context.register_llm_request_intercept("inject-intent", self.h1, priority=REQUEST_PRIORITY)
        context.register_tool_execution_intercept("authorize-execution", self.h3, priority=EXECUTION_PRIORITY)
        context.register_scope_sanitize_end_guardrail("export-annotation", sanitize_exported_annotation)

    def close(self) -> None:
        if self.store is not None:
            self.store.close()
            self.store = None

    # ── H1: the model request ────────────────────────────────────────────

    def h1(self, model_name: str, request: Any, annotated: Any) -> LlmRequestInterceptOutcome:
        guarded = self.settings.guard.matches
        field = nomos_field(self.evidence, self._citations_allowed)
        if annotated is not None:
            annotated = deepcopy(annotated)
            offered = annotated.get("tools") or []
            if offered:
                annotated["tools"], _ = inject_intent(offered, guarded, field)
            messages = annotated.get("messages") or []
        else:
            content = request.get("content") or {}
            offered = content.get("tools") or []
            tools, changed = inject_intent(offered, guarded, field)
            if changed:
                request = {**request, "content": {**content, "tools": tools}}
            messages = content.get("messages") or []
        tokens, visible = transcript_ids(messages)
        user_text = _latest_user_text(messages)
        # The tools as the application offered them, before the nomos field was added.
        recorded = offered if self._tool_definitions_on else None
        turn = self.store.open_turn(
            tokens,
            user_text[:MAX_USER_TEXT],
            recorded,
            user_request_truncated=len(user_text) > MAX_USER_TEXT,
        )
        entries = self._catalog(visible, turn) if self.evidence and self.settings.catalog.enabled else []
        request, annotated = append_message(request, annotated, render_message(turn.token, entries))
        return LlmRequestInterceptOutcome(request, annotated)

    def _catalog(self, visible: list[str], turn: Turn) -> list[dict]:
        cfg = self.settings.catalog
        return [
            catalog_entry(r, cfg.max_argument_chars)
            for r in self.store.catalog(visible, turn.citable, cfg.max_entries)
            if integrity_ok(r, self.signer.public_key_hex)
        ]

    @property
    def evidence(self) -> bool:
        """Whether this plugin keeps evidence: only with a configured signing key."""
        return self.signer is not None

    @property
    def _tool_definitions_on(self) -> bool:
        """Whether calls carry the observed tool definitions: they are certified, so only with a signing key."""
        return self.signer is not None and self.settings.tool_definitions != "off"

    @property
    def _citations_allowed(self) -> int:
        """Nomos takes at most MAX_CITATIONS certified items; the observed tool definitions take one of them."""
        return MAX_CITATIONS - 1 if self._tool_definitions_on else MAX_CITATIONS

    # ── H3: the tool call ────────────────────────────────────────────────

    async def h3(self, ctx: Any, next_: Any) -> ToolExecutionInterceptOutcome:
        if not self.settings.guard.matches(ctx.tool_name):
            inner = await next_.call(ctx.args)
            return ToolExecutionInterceptOutcome(inner.result, annotation=inner.annotation)
        started = time.monotonic()
        trace: dict[str, Any] = {"timings_ms": {}}
        try:
            out, extra = await self._guarded(ctx, next_, trace)
        except StoreError as exc:
            code = str(exc)
            execution = "unknown" if code in _UNKNOWN_STORE_ERRORS else "not_executed"
            out, extra = response(code, execution=execution), None
        except (sqlite3.Error, OSError, RecursionError):
            # Never expose database paths, service bodies, credentials or unverified output.
            out, extra = (
                response(
                    "storage_unavailable",
                    execution="unknown",
                    message="Evidence storage failed. Stop and reconcile this session before running anything else.",
                ),
                None,
            )
        except Exception:  # noqa: BLE001 — an unexpected failure must still close the gate
            out, extra = (
                response(
                    "plugin_error",
                    execution="unknown",
                    message="The governance plugin failed unexpectedly; treat this call as not approved.",
                ),
                None,
            )
        trace["timings_ms"]["total"] = (time.monotonic() - started) * 1000
        return self._out(out, trace=trace, extra=extra)

    async def _guarded(self, ctx: Any, next_: Any, trace: dict) -> tuple[dict, Any]:
        s, store = self.settings, self.store
        args = ctx.args
        try:
            size = len(canonical(args)) if isinstance(args, dict) else -1
        except (TypeError, ValueError, RecursionError):  # NaN, infinity, non-JSON or absurd nesting
            size = -1
        if not 0 <= size <= s.limits.max_result_bytes:
            message = "Tool arguments must be a bounded JSON object (no NaN or infinity)."
            return self._refuse(ctx, {}, "invalid_arguments", message), None
        # Canonical round-tripping creates an immutable-by-convention snapshot
        # that cannot share nested containers with Relay's mutable call object.
        arguments = json.loads(canonical({k: v for k, v in args.items() if k != "nomos"}))
        problems = intent_problems(
            args.get("nomos"), evidence=self.evidence, citations=self._citations_allowed
        )
        if problems:
            message = teach_nomos(ctx.tool_name, evidence=self.evidence) + " Problems: " + "; ".join(problems)
            return self._refuse(ctx, arguments, "missing_intent", message), None
        turn = store.turn(args["nomos"]["turn"])
        if turn is None:  # unknown, expired, or issued by another worker
            return self._refuse(ctx, arguments, "unknown_turn", TEACH_TURN.format(tool=ctx.tool_name)), None
        trace["conversation"] = turn.conversation
        if turn.conversation in self._busy:
            return response("session_busy", message="Serialize tool calls; this call was not run."), None
        self._busy.add(turn.conversation)
        try:
            return await self._in_conversation(ctx, next_, trace, turn, arguments)
        finally:
            self._busy.discard(turn.conversation)

    async def _in_conversation(
        self, ctx: Any, next_: Any, trace: dict, turn: Turn, arguments: dict
    ) -> tuple[dict, Any]:
        s, store, args, conversation = self.settings, self.store, ctx.args, turn.conversation
        call_id = ctx.tool_call_id
        if not isinstance(call_id, str) or not 1 <= len(call_id) <= 256:
            return self._refuse(
                ctx,
                arguments,
                "missing_tool_call_id",
                "The harness must supply a tool call id.",
                conversation,
            ), None
        policy = s.policy(ctx.tool_name)
        structured = None
        if policy.executor == "local":
            if not s.tool_call.subject:
                return self._refuse(
                    ctx,
                    arguments,
                    "missing_tool_subject",
                    "A host-derived tool_call.subject is required for local tool authorization.",
                    conversation,
                ), None
            try:
                structured = StructuredToolCall.build(ctx.tool_name, arguments, s.tool_call.subject)
            except ValueError as exc:
                return self._refuse(
                    ctx,
                    arguments,
                    str(exc),
                    (
                        "Structured tool argument integers exceed Nomos's 640-decimal-digit limit."
                        if str(exc) == "tool_integer_too_large"
                        else "Structured tool arguments exceed Nomos's 32,000-byte authorization limit."
                    ),
                    conversation,
                ), None
            command = None
        else:
            command = arguments.get(policy.command_argument)
            if (
                set(arguments) != {policy.command_argument}
                or not isinstance(command, str)
                or not command.strip()
            ):
                message = f"{ctx.tool_name} takes exactly one argument, {policy.command_argument!r}, a command string."
                return self._refuse(ctx, arguments, "invalid_command_arguments", message, conversation), None
        try:
            check_trust(s.trust.as_dict())
            await self._council_key()
        except EvidenceError as exc:
            return self._refuse(ctx, arguments, str(exc), TRUST_MESSAGES[str(exc)], conversation), None
        s = self.settings  # now holds the Council's key, if it was just fetched

        previous = store.begin(
            conversation, call_id, sha256_hex(canonical({"tool": ctx.tool_name, "args": args}))
        )
        if previous is not None:
            return previous, None  # a repeated call id with identical arguments replays its stored outcome

        ids = list(args["nomos"].get("evidence", []))
        try:
            certified = self._citations(ids, turn)
        except (EvidenceError, StoreError) as exc:
            return self._finish_refusal(
                ctx, arguments, str(exc), "A cited evidence id cannot be used.", conversation
            ), None
        context = f"tool={ctx.tool_name}; "
        if self._tool_definitions_on:
            # The certified definitions are those of the named turn, so it must be the one the call came from:
            # one no later request has shown. An earlier token would certify a list this call was not offered.
            if store.is_superseded(turn):
                message = TEACH_TURN.format(tool=ctx.tool_name)
                return self._finish_refusal(ctx, arguments, "unknown_turn", message, conversation), None
            observed = self._tool_definitions(ctx, turn)
            certified = [*certified, observed]
            # Nomos hashes `context` into the intent but not certified evidence: naming the item's digest here
            # binds the writ to the definitions the Council saw.
            context += f"tool definitions stdout_sha256={observed['evidence']['payload']['result']['stdout_sha256']}; "
        if store.denials(conversation) >= s.max_denials:
            message = "Too many consecutive council denials in this conversation; nothing was submitted."
            return self._finish_refusal(ctx, arguments, "denial_cap", message, conversation), None

        intent = args["nomos"]
        common_fields = {
            "agent_id": s.connection.agent_id,
            "auth_token": s.connection.auth_token,
            "council_id": s.connection.council_id,
            **{k: intent[k] for k in ("description", "justification", "target_resource", "blast_radius")},
            "evidence": [],  # nothing is asserted: every citation is certified
            "certified_evidence": certified,
            "context": (
                f"{context}latest user request: {turn.user_request}"
                + (" [user request truncated to 800 characters]" if turn.user_request_truncated else "")
            ),
        }
        if structured is None:
            fields = {
                **common_fields,
                "command": command,
                "working_directory": s.connection.working_directory,
            }
        else:
            # Shell fields are deliberately absent: Nomos v2 treats structured
            # authorization as a distinct, exact operation contract.
            fields = {
                **common_fields,
                "tool_call": structured.authorization(),
            }
        store.mark_citations(ids, "submitted_unknown")  # a failure cannot tell us what the council consumed
        requested_at = time.time()
        t0 = time.monotonic()
        try:
            decision = await self._council.authorize(**fields)
            if not isinstance(decision, dict):
                raise ValueError("council response is not an object")
        except Exception as exc:  # noqa: BLE001 — any failure is a closed gate
            decision = {"status": "error", "error": f"council unavailable ({type(exc).__name__})"}
        trace["timings_ms"]["council"] = (time.monotonic() - t0) * 1000
        # Host-only facts about what was asked of the Council, for observability (never secrets).
        trace["submitted"] = {
            **({"operation": structured.operation} if structured is not None else {"command": command}),
            "certified_citations": ids,
        }
        trace["council"] = {
            "status": decision.get("status"),
            "decision_id": decision.get("decision_id"),
            "votes": _votes(decision),
        }

        if decision.get("status") != "approved":
            return self._council_refusal(ctx, arguments, decision, conversation), None
        try:
            writ = (
                decode_tool_call_writ(decision.get("writ"))
                if structured is not None
                else decode_writ(decision.get("writ"))
            )
        except EvidenceError as exc:
            return self._finish_refusal(
                ctx, arguments, str(exc), "The council's writ is unreadable; nothing ran.", conversation
            ), None
        call = WritCall(
            ctx.tool_name,
            call_id,
            arguments,
            command,
            policy,
            decision.get("writ_id"),
            conversation,
            _continuation(next_),
            requested_at,
            structured,
        )
        t0 = time.monotonic()
        result = await self.handler.handle(call, decision["writ"], writ)
        trace["timings_ms"]["execute"] = (time.monotonic() - t0) * 1000
        if result.writ_valid:
            # A valid signed approval: the pinned Council validates every certified item before issuing one.
            store.mark_citations(ids, "accepted")
            store.set_denials(conversation, 0)
        out = {**result.response, "decision_id": decision.get("decision_id")}
        store.finish(conversation, call_id, out, result.record)
        return out, result.annotation

    async def _council_key(self) -> None:
        """With trust.council_public_key_source = "council": fetch the Council's key once per activation.

        The key is trusted from then on, as a pinned one would be: it is never re-fetched, so a later reply cannot
        swap it. A failed fetch refuses this call and is tried again on the next.
        """
        if self.settings.trust.council_public_key_hex:
            return
        async with self._key_lock:
            s = self.settings
            if s.trust.council_public_key_hex:
                return
            try:
                reply = await self._council.public_key(s.connection.council_id)
            except Exception as exc:
                raise EvidenceError("council_key_unavailable") from exc
            reply = reply if isinstance(reply, dict) else {}
            key = reply.get("public_key_hex")
            if (
                reply.get("error")
                or reply.get("council_id") != s.connection.council_id
                or not isinstance(key, str)
                or not re.fullmatch(r"[0-9a-f]{64}", key)
            ):
                raise EvidenceError("council_key_unavailable")
            self.settings = dataclasses.replace(
                s, trust=dataclasses.replace(s.trust, council_public_key_hex=key)
            )
            self.handler = load_writ_handler(
                self.settings, store=self.store, executor=self._executor, signer=self.signer
            )

    def _citations(self, ids: list[str], turn: Turn) -> list[dict]:
        """The cited records, as the certified ExecutionResults sent in ``certified_evidence``, each re-checked."""
        certified: list[dict] = []
        for evidence_id in ids:
            record = self.store.get(evidence_id, turn.citable)
            if not integrity_ok(record, self.signer.public_key_hex):
                raise StoreError("evidence_integrity_failure")
            if record["evidence_status"] == "invalid":
                raise EvidenceError("evidence_quarantined")
            value = citation(record)
            if value is None:  # a refusal or an unknown outcome: plugin-signed, never certified
                raise EvidenceError("evidence_not_citable")
            if record["kind"] == "executor_result":
                # Re-check against the current trust policy; a revoked key makes old evidence unusable.
                verify_executor_evidence(
                    value,
                    record["writ"],
                    self.settings.trust.as_dict(),
                    agent_id=self.settings.connection.agent_id,
                    council_id=self.settings.connection.council_id,
                )
            else:
                # Certified by this plugin's own key, which the integrity check above already pinned.
                verify_plugin_certified(
                    value,
                    self.signer.public_key_hex,
                    executor_id=self.signer.executor_id,
                    agent_id=self.settings.connection.agent_id,
                    council_id=self.settings.connection.council_id,
                    writ_id=record.get("writ_id") or "",
                )
            certified.append(value)
        if len(canonical(certified)) > self.settings.limits.max_citation_bytes:
            raise EvidenceError("citation_size_exceeded")
        return certified

    def _tool_definitions(self, ctx: Any, turn: Turn) -> dict:
        """The tool definitions offered in the call's own turn, as one certified evidence item.

        A Nomos ``ExecutionResult`` signed with ``signing_key``, whose stdout is the observation; certified by a
        Council that has registered the key as a trusted executor. ``called`` is null when the model was not
        offered the tool it called; it precedes ``tools`` so it survives the Council's cut of long stdout. A list
        the store did not keep (over ``limits.max_citation_bytes`` or the storage quota) is reported unavailable,
        with its digest only.
        """
        s = self.settings
        tools = self.store.tool_list(turn.tools_sha256) if turn.tools_kept else None
        observed: dict[str, Any] = {
            "type": "nomos.observed_tool_definitions",
            "version": 1,
            "agent_id": s.connection.agent_id,
            "council_id": s.connection.council_id,
            "tool_call_id": ctx.tool_call_id,
            "tool": ctx.tool_name,
            "tools_sha256": turn.tools_sha256,
            "signed_at": time.time(),
        }
        if tools is None:
            observed["unavailable"] = (
                "The plugin did not keep this tool list: it exceeded its storage limits."
            )
        else:
            observed["tool_count"] = len(tools)
            observed["called"] = next((t for t in tools if _tool_name(t) == ctx.tool_name), None)
            if s.tool_definitions == "all":
                observed["tools"] = tools
        assert self.signer is not None  # _tool_definitions_on
        return self.signer.certify(
            json.dumps(observed, separators=(",", ":"), allow_nan=False),
            command=TOOL_DEFINITIONS_COMMAND,
            agent_id=s.connection.agent_id,
            council_id=s.connection.council_id,
            request_id=ctx.tool_call_id,
        )

    # ── refusals and denials ─────────────────────────────────────────────

    def _record(self, ctx: Any, arguments: dict, kind: str, content: dict, evidence_id: str, **extra) -> dict:
        return plugin_record(
            evidence_id=evidence_id,
            kind=kind,
            tool_name=ctx.tool_name,
            tool_call_id=str(ctx.tool_call_id or ""),
            arguments=arguments,
            content=content,
            signer=self.signer,
            now=time.time(),
            **extra,
        )

    def _outcome(
        self,
        ctx: Any,
        arguments: dict,
        kind: str,
        status: str,
        *,
        record: dict | None = None,
        certify: dict | None = None,
        **fields: Any,
    ) -> tuple[dict, dict | None]:
        """A response the plugin produced, with its signed record. Without a signing key there is no record.

        ``certify`` (the Council's decision): the record also carries the plugin's certificate of it."""
        if not self.evidence:
            return response(status, **fields), None
        evidence_id = self.store.new_id()
        evidence = "plugin_certified" if certify is not None else "plugin_signed"
        out = response(status, evidence=evidence, evidence_id=evidence_id, **fields)
        extra = dict(record or {})
        if certify is not None:
            extra["certified_result"] = self._certify_decision(ctx, arguments, out["status"], certify)
        return out, self._record(ctx, arguments, kind, out, evidence_id, **extra)

    def _certify_decision(self, ctx: Any, arguments: dict, status: str, decision: dict) -> dict:
        """The Council's decision, as an ExecutionResult signed with ``signing_key``: its structured parts only.

        Nomos does not sign its decisions; this certifies that the plugin received this answer for this call. The
        members' and chairman's reasoning, votes, debate and error text are left out: they often quote the agent, and
        the certificate must not carry the model's claims back to the Council as the Council's. Each part is bounded
        as ``_render_denial`` shows it to the model. ``success`` is false: nothing was authorized."""
        assert self.signer is not None  # self.evidence
        s = self.settings
        guidance = decision.get("guidance") if isinstance(decision.get("guidance"), list) else []
        missing = decision.get("missing_permissions")
        missing = missing if isinstance(missing, list) else []
        decision_id = decision.get("decision_id")
        observed = {
            "type": "nomos.council_decision",
            "version": 1,
            "tool": ctx.tool_name,
            "tool_call_id": str(ctx.tool_call_id or ""),
            "decision_id": None if decision_id is None else bounded(str(decision_id), 256),
            "arguments_sha256": sha256_hex(canonical({k: v for k, v in arguments.items() if k != "nomos"})),
            "status": status,
            "guidance": [bounded(g, 1024) for g in guidance[:5] if isinstance(g, str) and g],
            "missing_permissions": [bounded(str(m), 256) for m in missing[:20]],
            "retryable": decision.get("retryable") is True,
        }
        return self.signer.certify(
            json.dumps(observed, separators=(",", ":"), allow_nan=False),
            command=COUNCIL_DECISION_COMMAND.format(tool=ctx.tool_name),
            agent_id=s.connection.agent_id,
            council_id=s.connection.council_id,
            request_id=str(ctx.tool_call_id or ""),
            success=False,
            error=f"the Council's answer ({status}) authorized nothing; no command ran",
        )

    def _refuse(
        self, ctx: Any, arguments: dict, status: str, message: str, conversation: str | None = None
    ) -> dict:
        """A refusal before the call is journaled: stored (with a signing key), never sent to the council.

        ``conversation`` is None while the call has not named a live turn."""
        out, record = self._outcome(ctx, arguments, "plugin_refusal", status, message=message)
        if record is not None:
            self.store.put(record, conversation)
        return out

    def _finish_refusal(
        self, ctx: Any, arguments: dict, status: str, message: str, conversation: str
    ) -> dict:
        out, record = self._outcome(ctx, arguments, "plugin_refusal", status, message=message)
        self.store.finish(conversation, ctx.tool_call_id, out, record)
        return out

    def _council_refusal(self, ctx: Any, arguments: dict, decision: dict, conversation: str) -> dict:
        denied = decision.get("status") == "denied"
        if denied:
            self.store.set_denials(conversation, self.store.denials(conversation) + 1)
        out, record = self._outcome(
            ctx,
            arguments,
            "council_denial" if denied else "council_error",
            "denied" if denied else "council_error",
            record={"decision_id": decision.get("decision_id")},
            certify=decision,
            message=_render_denial(decision),
            council=_bounded_json({k: v for k, v in decision.items() if k != "writ"}),
        )
        self.store.finish(conversation, ctx.tool_call_id, out, record)
        return out

    @staticmethod
    def _out(result: dict, *, trace: dict | None = None, extra: Any = None) -> ToolExecutionInterceptOutcome:
        """The tool result plus a host-only ``nomos`` annotation (never shown to the model)."""
        nomos = {k: result[k] for k in _ANNOTATED_KEYS if k in result}
        if trace:
            nomos.update({k: v for k, v in trace.items() if k != "timings_ms"})
            nomos["timings_ms"] = {k: round(v, 1) for k, v in trace["timings_ms"].items()}
        annotation = {"nomos": nomos}
        if extra is not None:
            annotation["tool"] = extra
        return ToolExecutionInterceptOutcome(result, annotation=annotation)


def sanitize_exported_annotation(_event: Any, fields: dict) -> dict:
    """What subscribers and exporters see of the ``{"nomos": …}`` annotation on a tool's end event.

    Only the status fields (``_ANNOTATED_KEYS``), the Council's status and decision id, and the timings. The submitted
    command or operation, the Council's votes and the conversation id stay with the host, which gets the whole
    annotation from ``tools.execute``. Pure: Relay calls it for every scope end event, and returns all three fields.
    """
    profile = fields.get("category_profile")
    annotation = profile.get("tool_result_annotation") if isinstance(profile, dict) else None
    if isinstance(annotation, dict) and isinstance(annotation.get("nomos"), dict):
        full = annotation["nomos"]
        nomos = {k: v for k, v in full.items() if k in (*_ANNOTATED_KEYS, "timings_ms")}
        if isinstance(full.get("council"), dict):
            nomos["council"] = {k: v for k, v in full["council"].items() if k in ("status", "decision_id")}
        profile = {**profile, "tool_result_annotation": {**annotation, "nomos": nomos}}
    return {"data": fields.get("data"), "category_profile": profile, "metadata": fields.get("metadata")}


def _votes(decision: dict) -> list[dict]:
    members = decision.get("council_members")
    if not isinstance(members, list):
        return []
    return [
        {"member_id": m.get("member_id"), "decision": m.get("decision")}
        for m in members[:20]
        if isinstance(m, dict)
    ]


def _continuation(next_: Any):
    async def call(arguments: dict):
        return await next_.call(arguments)

    return call


def _tool_name(tool: Any) -> Any:
    """A tool definition's name: nested (OpenAI chat, codec form), flat (Anthropic, OpenAI Responses), or the
    provider's own definition inside a codec ``provider_native`` tool."""
    if not isinstance(tool, dict):
        return None
    if isinstance(tool.get("function"), dict):
        return tool["function"].get("name")
    if tool.get("type") == "provider_native" and isinstance(tool.get("value"), dict):
        return tool["value"].get("name")
    return tool.get("name")


def _latest_user_text(messages: list) -> str:
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(
                p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
            )
    return ""


def _bounded_json(value: Any, *, max_chars: int = 8192, max_items: int = 50, depth: int = 0) -> Any:
    """The Council's response, kept whole but bounded so a hostile body cannot flood the transcript."""
    if depth > 8:
        return "…"
    if isinstance(value, str):
        return bounded(value, max_chars)
    if isinstance(value, list):
        items = [
            _bounded_json(v, max_chars=max_chars, max_items=max_items, depth=depth + 1)
            for v in value[:max_items]
        ]
        return items + ([f"…[{len(value) - max_items} more]"] if len(value) > max_items else [])
    if isinstance(value, dict):
        return {
            str(k)[:256]: _bounded_json(v, max_chars=max_chars, max_items=max_items, depth=depth + 1)
            for k, v in list(value.items())[:max_items]
        }
    return value if isinstance(value, (int, float, bool)) or value is None else repr(value)


def _render_denial(decision: dict) -> str:
    status = str(decision.get("status") or "error").upper()
    lines = [f"Not run: the oversight council returned {status}."]
    if isinstance(decision.get("error"), str) and decision["error"]:
        lines.append(f"Reason: {bounded(decision['error'], 1024)}")
    guidance = decision.get("guidance")
    if isinstance(guidance, list):
        lines.extend(f"Guidance: {bounded(g, 1024)}" for g in guidance[:5] if isinstance(g, str) and g)
    missing = decision.get("missing_permissions")
    if isinstance(missing, list) and missing:
        lines.append("Missing permissions: " + ", ".join(map(str, missing[:20])))
    if decision.get("retryable"):
        lines.append("This refusal is retryable later.")
    return "\n".join(lines)
