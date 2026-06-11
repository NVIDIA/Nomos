"""
LLM-backed council member agent.

Each council member is an LLM agent that evaluates intents
based on configured priorities. This is the bridge between
the LLM backend and the SecurityCouncil voting system.
"""

import asyncio
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel, Field, ValidationError, field_validator

from nomos.config.settings import CouncilMemberConfig, LLMBackendConfig, LLMProvider
from nomos.level_0.types import Intent
from nomos.level_1.policy.paths import references_sensitive_path
from nomos.level_1.policy.shell import shell_redirection_writes_to_disk

from .base import (
    LLMBackend,
    LLMMessage,
    LLMRateLimitError,
    LLMResponse,
    LLMRole,
    LLMTimeoutError,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from .limiter import LimiterPool

logger = logging.getLogger(__name__)

T = TypeVar("T")

MAX_RETRIES = 2
RETRY_DELAY_SECONDS = 1.0
DEFAULT_RETRY_BUDGET_SECONDS = 40.0
DEFAULT_MAX_RETRY_AFTER_SECONDS = 10.0
DEFAULT_MEMBER_MAX_TOKENS = 1024
DEFAULT_CHAIRMAN_MAX_TOKENS = 512
DEFAULT_CODE_REVIEW_MAX_TOKENS = 2048


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000


def _token_count(usage: dict[str, Any]) -> int:
    total = usage.get("total_tokens")
    if isinstance(total, int):
        return total
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0))
    return int(input_tokens or 0) + int(output_tokens or 0)


class IntentEvaluation(BaseModel):
    """Structured evaluation result from an LLM council member."""

    decision: str = Field(..., description="'approve' or 'deny'")
    reasoning: str = Field(default="", description="Detailed reasoning")
    risk_concerns: list[str] = Field(default_factory=list, description="Specific risk concerns")
    conditions: list[str] = Field(default_factory=list, description="Conditions for approval")
    guidance: str = Field(
        default="", description="How the request could be narrowed or improved if denied"
    )

    @field_validator("guidance", mode="before")
    @classmethod
    def coerce_guidance(cls, v: Any) -> str:
        """Coerce null/non-string guidance to empty string."""
        if v is None:
            return ""
        return str(v)


class CodeReviewFileAssessment(BaseModel):
    """Required per-file assessment returned by the Code Review member."""

    path: str = Field(..., min_length=1, description="Packaged file path reviewed")
    sha256: str = Field(..., min_length=64, max_length=64, description="Expected SHA-256")
    mode: str = Field(..., min_length=3, description="Packaged file mode")
    review_status: str = Field(..., description="'complete', 'truncated', or 'inconclusive'")
    matches_claimed_behavior: str = Field(..., description="'yes', 'partial', 'no', or 'unclear'")
    observed_operations: list[str] = Field(
        ..., min_length=1, description="Concrete operations observed in the file"
    )
    risk_findings: list[str] = Field(..., description="Concrete risks found; empty list if none")
    evidence: list[str] = Field(
        ..., min_length=1, description="Specific code evidence supporting the assessment"
    )

    @field_validator("sha256", mode="before")
    @classmethod
    def normalize_sha256(cls, v: Any) -> str:
        return str(v or "").lower().strip()

    @field_validator("review_status", mode="before")
    @classmethod
    def normalize_review_status(cls, v: Any) -> str:
        value = str(v or "").lower().strip()
        if value not in {"complete", "truncated", "inconclusive"}:
            raise ValueError("review_status must be complete, truncated, or inconclusive")
        return value

    @field_validator("matches_claimed_behavior", mode="before")
    @classmethod
    def normalize_match(cls, v: Any) -> str:
        value = str(v or "").lower().strip()
        if value not in {"yes", "partial", "no", "unclear"}:
            raise ValueError("matches_claimed_behavior must be yes, partial, no, or unclear")
        return value


class CodeReviewEvaluation(IntentEvaluation):
    """Structured Code Review result that voting members can inspect clearly."""

    decision: str = Field(..., description="'approve' or 'deny'")
    reasoning: str = Field(..., min_length=1, description="Clear overall explanation")
    reviewed_all_content: bool = Field(..., description="Whether all attached bytes were reviewed")
    claim_match: str = Field(..., description="'yes', 'partial', 'no', or 'unclear'")
    overall_risk: str = Field(..., description="'low', 'medium', 'high', or 'critical'")
    files: list[CodeReviewFileAssessment] = Field(
        ..., min_length=1, description="One assessment for each packaged file"
    )
    risk_concerns: list[str] = Field(
        ..., description="Specific cross-file or overall concerns; empty list if none"
    )
    conditions: list[str] = Field(
        ..., description="Conditions required for approval; empty list if none"
    )
    guidance: str = Field(
        ..., description="If denying, how to make the packaged file acceptable; else empty"
    )

    @field_validator("decision", mode="before")
    @classmethod
    def normalize_decision(cls, v: Any) -> str:
        value = str(v or "").lower().strip()
        if value not in {"approve", "deny"}:
            raise ValueError("decision must be approve or deny")
        return value

    @field_validator("reasoning", mode="before")
    @classmethod
    def validate_reasoning(cls, v: Any) -> str:
        value = str(v or "").strip()
        if not value or value.lower() in {"null", "none", "n/a", "na", "see above"}:
            raise ValueError("reasoning must be specific and non-empty")
        return value

    @field_validator("claim_match", mode="before")
    @classmethod
    def normalize_claim_match(cls, v: Any) -> str:
        value = str(v or "").lower().strip()
        if value not in {"yes", "partial", "no", "unclear"}:
            raise ValueError("claim_match must be yes, partial, no, or unclear")
        return value

    @field_validator("overall_risk", mode="before")
    @classmethod
    def normalize_overall_risk(cls, v: Any) -> str:
        value = str(v or "").lower().strip()
        if value not in {"low", "medium", "high", "critical"}:
            raise ValueError("overall_risk must be low, medium, high, or critical")
        return value


class ChairmanDecision(BaseModel):
    """The chairman's decision on voting threshold for an intent."""

    threshold: str = Field(..., description="'quorum' (majority) or 'unanimous'")
    reasoning: str = Field(default="", description="Why this threshold was chosen")
    risk_level: str = Field(default="medium", description="'low', 'medium', 'high', 'critical'")

    @field_validator("threshold", mode="before")
    @classmethod
    def normalize_threshold(cls, v: Any) -> str:
        if v is None:
            return "unanimous"
        v = str(v).lower().strip()
        if v not in ("quorum", "unanimous"):
            return "unanimous"  # default to stricter threshold on unknown
        return v  # type: ignore[no-any-return]

    @field_validator("risk_level", mode="before")
    @classmethod
    def normalize_risk(cls, v: Any) -> str:
        v = str(v or "medium").lower().strip()
        if v not in ("low", "medium", "high", "critical"):
            return "medium"
        return v  # type: ignore[no-any-return]


class ChairmanPreAuthDecision(BaseModel):
    """Result of the chairman's two-phase pre-authorization check."""

    authorized: bool = Field(..., description="Whether the agent is authorized to proceed")
    threshold: str | None = Field(
        default=None, description="'quorum' or 'unanimous' (None if not authorized)"
    )
    risk_level: str = Field(default="medium", description="'low', 'medium', 'high', 'critical'")
    reasoning: str = Field(default="", description="Explanation of the decision")
    missing_permissions: list[str] = Field(
        default_factory=list, description="Permissions the agent lacks"
    )

    @field_validator("threshold", mode="before")
    @classmethod
    def normalize_threshold(cls, v: Any) -> str | None:
        if v is None:
            return None
        v = str(v).lower().strip()
        if v not in ("quorum", "unanimous"):
            return "unanimous"
        return v  # type: ignore[no-any-return]

    @field_validator("risk_level", mode="before")
    @classmethod
    def normalize_risk(cls, v: Any) -> str:
        v = str(v or "medium").lower().strip()
        if v not in ("low", "medium", "high", "critical"):
            return "medium"
        return v  # type: ignore[no-any-return]


def _backend_class(provider: LLMProvider) -> type[Any]:
    if provider == LLMProvider.OPENAI:
        from .openai_backend import OpenAIBackend

        return OpenAIBackend
    elif provider == LLMProvider.ANTHROPIC:
        from .anthropic_backend import AnthropicBackend

        return AnthropicBackend
    elif provider == LLMProvider.NIM:
        from .nim_backend import NIMBackend

        return NIMBackend
    elif provider == LLMProvider.NV_INFERENCE:
        from .nv_inference_backend import NVInferenceBackend

        return NVInferenceBackend
    else:
        raise ValueError(f"Unknown LLM provider: {provider}")


def create_backend(
    config: LLMBackendConfig,
    limiter_pool: "LimiterPool | None" = None,
) -> LLMBackend:
    """Factory to create an LLM backend from config.

    When `limiter_pool` is provided, the backend is wrapped so completions
    share the in-flight cap for its endpoint. Multiple backend names that
    target the same (provider, base_url) — common with NVCF model routing —
    resolve to one limiter, so the cap bounds aggregate provider load.
    """
    backend_cls = _backend_class(config.provider)
    backend: LLMBackend = backend_cls(
        api_key=config.api_key,
        model=config.model,
        base_url=config.base_url or None,
        timeout=config.request_timeout_seconds,
        max_retries=config.sdk_max_retries,
    )
    if limiter_pool is None:
        return backend

    from .limiter import LimitedBackend

    effective_base_url = config.base_url or getattr(backend_cls, "DEFAULT_BASE_URL", "")
    limiter = limiter_pool.get(
        config.provider.value,
        effective_base_url,
        max_concurrent=config.max_concurrent,
        queue_reject_threshold=config.queue_reject_threshold,
    )
    return LimitedBackend(backend, limiter)


CERTIFIED_EVIDENCE_LABEL = "CERTIFIED EXECUTOR EVIDENCE"
AGENT_ASSERTED_EVIDENCE_LABEL = "AGENT-ASSERTED EVIDENCE"


def _defang_agent_text(text: str) -> str:
    """Stop agent-supplied text from impersonating the certified-evidence header.

    The trust label is now structural (which channel the item came from), so a
    forged header can no longer flip the label. This is defense-in-depth: it also
    prevents raw text from *reading* as a genuine certified block to the LLM
    inside a correctly-labeled AGENT-ASSERTED section.
    """
    if CERTIFIED_EVIDENCE_LABEL in text:
        return text.replace(
            CERTIFIED_EVIDENCE_LABEL,
            "CERTIFIED-EXECUTOR-EVIDENCE (agent-claimed, NOT verified)",
        )
    return text


def _format_intent_for_review(intent: Intent) -> str:
    """
    Format an intent into a human-readable review prompt.

    Parameters
    ----------
    intent : Intent
        The intent to format for review.

    Returns
    -------
    str
        The formatted markdown string representing the intent.
    """
    parts = [
        "# Intent Review Request",
        "",
        f"**Intent ID:** {intent.intent_id}",
        f"**Agent:** {intent.agent_id}",
        f"**Action Type:** {intent.action_type}",
        f"**Description:** {intent.description}",
        f"**Target Resource:** {intent.target_resource}",
    ]

    if intent.command:
        parts.append("\n## Command")
        parts.append(f"**Type:** {intent.command_type}")
        parts.append(f"```\n{intent.command}\n```")
        # Always render cwd — an unset value is a signal reviewers should see.
        wd = intent.working_directory or "*(not specified)*"
        parts.append(f"**Working Directory:** {wd}")

    if intent.target_host:
        parts.append(f"\n**Target Host:** {intent.target_host}")
    if intent.target_environment:
        parts.append(f"**Target Environment:** {intent.target_environment}")
    if intent.target_cluster:
        parts.append(f"**Target Cluster:** {intent.target_cluster}")
    if intent.target_namespace:
        parts.append(f"**Target Namespace:** {intent.target_namespace}")
    if intent.target_container:
        parts.append(f"**Target Container:** {intent.target_container}")

    if intent.packaged_files:
        parts.append("\n## Packaged Files")
        parts.extend(
            [
                "These files are attached to the structured intent. Their bytes,",
                "destination path, mode, and SHA-256 digest are part of the intent",
                "record and are bound into the signed writ if the request is approved.",
                "The Code Review council member receives the decoded file contents before",
                "voting members review this request. Voting members should use the Code",
                "Review Analysis in the context section as file-content evidence and must",
                "not require the requester to paste the full file contents inline solely",
                "because a packaged-file workflow is being used.",
                "",
            ]
        )
        for packaged_file in intent.packaged_files:
            parts.append(
                f"  - `{packaged_file.path}` sha256={packaged_file.sha256} "
                f"mode={packaged_file.mode}"
            )
            if packaged_file.description:
                parts.append(f"    Purpose: {packaged_file.description}")

    if intent.justification:
        parts.append(f"\n## Justification\n{intent.justification}")

    if intent.blast_radius:
        parts.append(f"\n## Blast Radius\n{intent.blast_radius}")

    if intent.context:
        parts.append(f"\n## Context\n{intent.context}")

    if intent.preceding_commands:
        parts.append("\n## Preceding Commands")
        for cmd in intent.preceding_commands:
            parts.append(f"  - `{cmd}`")

    if intent.rollback_plan:
        parts.append("\n## Rollback Plan")
        parts.append(f"**Strategy:** {intent.rollback_plan.strategy}")
        if intent.rollback_plan.steps:
            for step in intent.rollback_plan.steps:
                parts.append(f"  - {step}")

    # Trust comes from which channel the evidence arrived on, never from its text.
    # certified_evidence_rendered is populated only by the council's verification
    # path (signature/key/hash checks); intent.evidence is raw agent claims.
    labeled_evidence: list[tuple[str, str]] = [
        (CERTIFIED_EVIDENCE_LABEL, text) for text in intent.certified_evidence_rendered
    ] + [(AGENT_ASSERTED_EVIDENCE_LABEL, _defang_agent_text(text)) for text in intent.evidence]
    if labeled_evidence:
        parts.append("\n## Evidence")
        parts.extend(
            [
                "Evidence trust levels are explicit:",
                "- Items labeled CERTIFIED EXECUTOR EVIDENCE were verified by the council",
                "  service before review: executor signature, registered public key,",
                "  signed command/result payload, and stdout/stderr hashes matched.",
                "- Items labeled AGENT-ASSERTED EVIDENCE are requester-provided claims.",
                "  They may be useful context, but they are not cryptographic proof.",
            ]
        )
        for idx, (label, text) in enumerate(labeled_evidence, 1):
            parts.append(f"\n### Evidence {idx}: {label}")
            parts.append("```text")
            parts.append(text)
            parts.append("```")

    return "\n".join(parts)


def _format_packaged_files_for_review(intent: Intent, max_file_bytes: int) -> str:
    """Render packaged file contents for the Code Review member."""
    parts = [
        "# Packaged File Code Review",
        "",
        _format_intent_for_review(intent),
        "",
        "## Packaged Files",
    ]
    for packaged_file in intent.packaged_files:
        text, truncated = packaged_file.text_for_review(max_file_bytes)
        parts.extend(
            [
                "",
                f"### {packaged_file.path}",
                f"**SHA-256:** {packaged_file.sha256}",
                f"**Mode:** {packaged_file.mode}",
                f"**Description:** {packaged_file.description or '*(not specified)*'}",
                f"**Decoded Bytes:** {len(packaged_file.decoded_bytes())}",
            ]
        )
        if truncated:
            parts.append(
                f"**Review Note:** content is truncated to {max_file_bytes} bytes. "
                "If the omitted content is needed to verify behavior, deny the request."
            )
        parts.append("```")
        parts.append(text)
        parts.append("```")

    return "\n".join(parts)


def format_code_review_analysis(review: IntentEvaluation) -> str:
    """Render Code Review output for subsequent council members."""
    structured_json = format_code_review_result_json(review)
    parts = [
        "## Code Review Analysis",
        "Code Review inspected the decoded packaged file contents before the voting",
        "members received this request. Treat this as the file-content review for",
        "packaged files; do not ask for the full file to be pasted inline unless",
        "this analysis is absent, denied, truncated, or inconclusive.",
        "",
        "Structured JSON result:",
        "```json",
        structured_json,
        "```",
    ]
    return "\n".join(parts)


def format_code_review_result_json(review: IntentEvaluation) -> str:
    """Return the Code Review result as stable, pretty JSON."""
    return json.dumps(review.model_dump(mode="json"), indent=2, sort_keys=True)


class _RetryExhaustedError(Exception):
    """All completion attempts failed; carries the count for failure messages."""

    def __init__(self, attempts: int, last_error: Exception):
        super().__init__(str(last_error))
        self.attempts = attempts
        self.last_error = last_error


async def _complete_with_retry(
    backend: LLMBackend,
    messages: list[LLMMessage],
    *,
    temperature: float,
    max_tokens: int,
    parse: "Callable[[str], T]",
    log_ctx: str,
    max_retries: int = MAX_RETRIES,
    retry_budget_seconds: float = DEFAULT_RETRY_BUDGET_SECONDS,
    max_retry_after_seconds: float = DEFAULT_MAX_RETRY_AFTER_SECONDS,
) -> tuple["T", LLMResponse, int, float]:
    """Run a JSON completion with bounded retries.

    Rate-limit errors sleep min(provider Retry-After, cap); parse and other
    transient errors back off linearly. Retrying stops at `max_retries` or
    when the next sleep would exceed `retry_budget_seconds`. Timeouts are
    never retried: a timed-out call already held a concurrency slot for the
    full request timeout, so retrying multiplies load on a provider that is
    already slow and blows through the caller's deadline.

    Returns (parsed result, raw response, attempt index, call latency ms);
    raises _RetryExhaustedError when no attempt succeeded.
    """
    start = time.monotonic()
    invalid_response = ""
    attempt = 0
    while True:
        response_content = ""
        try:
            call_start = time.perf_counter()
            response = await backend.complete(
                messages=_messages_for_attempt(messages, invalid_response),
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
            response_content = response.content
            return parse(response_content), response, attempt, _elapsed_ms(call_start)
        except LLMTimeoutError as e:
            logger.error("%s timed out: %s — not retrying", log_ctx, e)
            raise _RetryExhaustedError(attempt + 1, e) from e
        except Exception as e:
            if response_content:
                invalid_response = response_content
            delay = RETRY_DELAY_SECONDS * (attempt + 1)
            if isinstance(e, LLMRateLimitError) and e.retry_after is not None:
                delay = min(e.retry_after, max_retry_after_seconds)
            out_of_budget = time.monotonic() - start + delay > retry_budget_seconds
            if attempt >= max_retries or out_of_budget:
                logger.error(
                    "%s failed after %d attempts%s: %s",
                    log_ctx,
                    attempt + 1,
                    " (retry budget exhausted)" if out_of_budget and attempt < max_retries else "",
                    e,
                )
                raise _RetryExhaustedError(attempt + 1, e) from e
            logger.warning(
                "%s attempt %d/%d failed: %s — retrying",
                log_ctx,
                attempt + 1,
                1 + max_retries,
                e,
            )
            await asyncio.sleep(delay)
            attempt += 1


class CouncilMemberAgent:
    """
    An LLM-backed council member that evaluates intents.

    Each agent has:
    - An LLM backend for inference
    - A system prompt built from configured priorities
    - An evaluate method that returns a structured decision
    """

    def __init__(
        self,
        member_config: CouncilMemberConfig,
        backend: LLMBackend,
        *,
        temperature: float = 0.0,
        max_tokens: int = DEFAULT_MEMBER_MAX_TOKENS,
        max_retries: int = MAX_RETRIES,
        retry_budget_seconds: float = DEFAULT_RETRY_BUDGET_SECONDS,
        max_retry_after_seconds: float = DEFAULT_MAX_RETRY_AFTER_SECONDS,
    ):
        self._config = member_config
        self._backend = backend
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._max_retries = max_retries
        self._retry_budget_seconds = retry_budget_seconds
        self._max_retry_after_seconds = max_retry_after_seconds
        self._system_prompt = member_config.build_system_prompt()

    @property
    def member_id(self) -> str:
        return self._config.id

    @property
    def weight(self) -> int:
        return self._config.weight

    @property
    def display_name(self) -> str:
        return self._config.display_name or self._config.id

    @property
    def provider_name(self) -> str:
        return self._backend.provider_name()

    async def evaluate_intent(self, intent: Intent) -> IntentEvaluation:
        """
        Evaluate an intent and return a structured decision.

        The LLM receives the full intent details and responds
        with approve/deny plus reasoning. Retries on failure.

        Parameters
        ----------
        intent : Intent
            The intent to evaluate.

        Returns
        -------
        IntentEvaluation
            The structured evaluation containing decision and reasoning.
        """
        review_prompt = _format_intent_for_review(intent)

        messages = [
            LLMMessage(role=LLMRole.SYSTEM, content=self._system_prompt),
            LLMMessage(role=LLMRole.USER, content=review_prompt),
        ]

        try:
            evaluation, response, attempt, call_ms = await _complete_with_retry(
                self._backend,
                messages,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                parse=lambda content: _parse_evaluation(content, raise_on_parse_error=True),
                log_ctx=f"Council member {self.member_id} evaluating {intent.intent_id}",
                max_retries=self._max_retries,
                retry_budget_seconds=self._retry_budget_seconds,
                max_retry_after_seconds=self._max_retry_after_seconds,
            )
        except _RetryExhaustedError as e:
            return IntentEvaluation(
                decision="deny",
                reasoning=(
                    f"Evaluation failed after {e.attempts} attempts:"
                    f" {e.last_error}. Defaulting to deny."
                ),
                risk_concerns=["LLM evaluation failure"],
            )

        logger.info(
            "llm_latency role=council_member member=%s provider=%s intent=%s attempt=%d "
            "ms=%.1f tokens=%d decision=%s",
            self.member_id,
            self._backend.provider_name(),
            intent.intent_id,
            attempt + 1,
            call_ms,
            _token_count(response.usage),
            evaluation.decision,
        )
        return evaluation

    async def debate_evaluate(
        self,
        intent: Intent,
        debate_context: str,
    ) -> IntentEvaluation:
        """
        Re-evaluate an intent with debate context using this member's own backend.

        Each member deliberates with the same LLM it used for the initial
        evaluation, preserving model diversity across the council.

        The conversation is:
          1. System prompt (this member's priorities)
          2. Original intent review
          3. Debate context (all members' Round N positions)

        The member may change or maintain their vote.

        Parameters
        ----------
        intent : Intent
            The intent being debated.
        debate_context : str
            The formatted debate context containing previous votes.

        Returns
        -------
        IntentEvaluation
            The new structured evaluation after considering the debate.
        """
        review_prompt = _format_intent_for_review(intent)
        debate_message = f"{review_prompt}\n\n---\n\n{debate_context}"

        messages = [
            LLMMessage(role=LLMRole.SYSTEM, content=self._system_prompt),
            LLMMessage(role=LLMRole.USER, content=debate_message),
        ]

        try:
            evaluation, response, attempt, call_ms = await _complete_with_retry(
                self._backend,
                messages,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                parse=lambda content: _parse_evaluation(content, raise_on_parse_error=True),
                log_ctx=f"Council member {self.member_id} debating {intent.intent_id}",
                max_retries=self._max_retries,
                retry_budget_seconds=self._retry_budget_seconds,
                max_retry_after_seconds=self._max_retry_after_seconds,
            )
        except _RetryExhaustedError as e:
            return IntentEvaluation(
                decision="deny",
                reasoning=(
                    f"Debate evaluation failed after {e.attempts} attempts:"
                    f" {e.last_error}. Defaulting to deny."
                ),
                risk_concerns=["LLM debate evaluation failure"],
            )

        logger.info(
            "llm_latency role=council_member_debate member=%s provider=%s intent=%s "
            "attempt=%d ms=%.1f tokens=%d decision=%s",
            self.member_id,
            self._backend.provider_name(),
            intent.intent_id,
            attempt + 1,
            call_ms,
            _token_count(response.usage),
            evaluation.decision,
        )
        return evaluation


class CodeReviewAgent:
    """LLM-backed reviewer for packaged file contents."""

    def __init__(
        self,
        backend: LLMBackend,
        *,
        display_name: str = "Code Review",
        system_prompt: str = "",
        max_file_bytes: int = 131072,
        temperature: float = 0.0,
        max_tokens: int = DEFAULT_CODE_REVIEW_MAX_TOKENS,
        max_retries: int = MAX_RETRIES,
        retry_budget_seconds: float = DEFAULT_RETRY_BUDGET_SECONDS,
        max_retry_after_seconds: float = DEFAULT_MAX_RETRY_AFTER_SECONDS,
    ):
        self._backend = backend
        self._display_name = display_name
        self._max_file_bytes = max_file_bytes
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._max_retries = max_retries
        self._retry_budget_seconds = retry_budget_seconds
        self._max_retry_after_seconds = max_retry_after_seconds
        self._system_prompt = system_prompt or self._default_system_prompt()

    @property
    def member_id(self) -> str:
        return "code-review"

    @property
    def display_name(self) -> str:
        return self._display_name

    @property
    def provider_name(self) -> str:
        return self._backend.provider_name()

    @staticmethod
    def _default_system_prompt() -> str:
        return "\n".join(
            [
                "You are the Code Review council member for packaged execution files.",
                "Your task is to inspect the packaged file contents before any voting member",
                "reviews the request. Determine whether the file contents materially do what",
                "the requesting agent claims in the intent description, justification, command,",
                "and blast radius.",
                "",
                "Focus on hidden behavior, destructive operations, data exfiltration, network",
                "access, privilege escalation, dynamic code loading, obfuscation, and mismatch",
                "between the file and the asserted purpose. If content is truncated or cannot",
                "be confidently reviewed, deny and explain the uncertainty.",
                "",
                "Your analysis is the file-content evidence provided to the voting council",
                "members. Do not ask them to require the requester to paste the full file",
                "inline; you already have the decoded packaged file content. For each file,",
                "cite the path, SHA-256 digest, whether the content was fully reviewed, and",
                "the concrete operations you observed, especially file writes/deletes,",
                "network calls, subprocess execution, secret reads, dynamic imports, or",
                "path traversal.",
                "",
                "Your response must make the evaluation completely clear. Return exactly",
                "one JSON object, with every field shown below present. No markdown fences,",
                "no prose outside JSON, and no omitted fields.",
                "{",
                '  "decision": "approve",',
                '  "reviewed_all_content": true,',
                '  "claim_match": "yes",',
                '  "overall_risk": "low",',
                '  "reasoning": "at least two specific sentences about what the files do",',
                '  "files": [',
                "    {",
                '      "path": "scripts/example.py",',
                '      "sha256": "exact SHA-256 digest from the prompt",',
                '      "mode": "0600",',
                '      "review_status": "complete",',
                '      "matches_claimed_behavior": "yes",',
                '      "observed_operations": ["writes /workspace/out.txt"],',
                '      "risk_findings": [],',
                '      "evidence": ["line or code reference supporting the assessment"]',
                "    }",
                "  ],",
                '  "risk_concerns": ["specific concerns if any"],',
                '  "conditions": ["conditions for safe approval if any"],',
                '  "guidance": "if denying, explain what must change"',
                "}",
                "",
                "Allowed values:",
                '- decision: "approve" or "deny"',
                '- claim_match and matches_claimed_behavior: "yes", "partial", "no", or "unclear"',
                '- overall_risk: "low", "medium", "high", or "critical"',
                '- review_status: "complete", "truncated", or "inconclusive"',
                "",
                "Include exactly one files[] entry for each packaged file in the prompt.",
                "The path, sha256, and mode values must exactly match the packaged file",
                "metadata shown in the prompt. If any file was truncated, unavailable, or",
                'unclear, set reviewed_all_content=false and decision="deny".',
            ]
        )

    async def review_intent_files(self, intent: Intent) -> IntentEvaluation:
        """Review packaged files and return advisory analysis."""
        if not intent.packaged_files:
            return IntentEvaluation(
                decision="approve",
                reasoning="No packaged files were attached, so no file-content review was needed.",
            )

        review_prompt = _format_packaged_files_for_review(intent, self._max_file_bytes)
        messages = [
            LLMMessage(role=LLMRole.SYSTEM, content=self._system_prompt),
            LLMMessage(role=LLMRole.USER, content=review_prompt),
        ]

        try:
            evaluation, response, attempt, call_ms = await _complete_with_retry(
                self._backend,
                messages,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                parse=lambda content: _parse_code_review_evaluation(content, intent),
                log_ctx=f"Code reviewer evaluating {intent.intent_id}",
                max_retries=self._max_retries,
                retry_budget_seconds=self._retry_budget_seconds,
                max_retry_after_seconds=self._max_retry_after_seconds,
            )
        except _RetryExhaustedError as e:
            return _failed_code_review_evaluation(
                intent,
                f"Code review failed after {e.attempts} attempts: {e.last_error}.",
            )

        logger.info(
            "llm_latency role=code_review provider=%s intent=%s attempt=%d "
            "ms=%.1f tokens=%d decision=%s",
            self._backend.provider_name(),
            intent.intent_id,
            attempt + 1,
            call_ms,
            _token_count(response.usage),
            evaluation.decision,
        )
        return evaluation


class _CodeReviewFormatError(ValueError):
    """Raised when Code Review returns JSON that does not meet its schema."""


def _parse_code_review_evaluation(content: str, intent: Intent) -> CodeReviewEvaluation:
    """Parse and validate the structured Code Review JSON result."""
    try:
        data = _load_json_object(content)
        evaluation = CodeReviewEvaluation.model_validate(data)
    except (json.JSONDecodeError, TypeError, ValidationError, ValueError) as e:
        logger.warning(
            "Failed to parse Code Review response: %s — raw content: %r",
            e,
            content[:500],
        )
        raise _CodeReviewFormatError(str(e)) from e

    expected = {packaged_file.path: packaged_file for packaged_file in intent.packaged_files}
    returned_paths = {file.path for file in evaluation.files}
    expected_paths = set(expected)
    if returned_paths != expected_paths:
        raise _CodeReviewFormatError(
            f"Code Review files mismatch: expected {sorted(expected_paths)}, "
            f"got {sorted(returned_paths)}"
        )

    for file in evaluation.files:
        packaged_file = expected[file.path]
        if file.sha256 != packaged_file.sha256:
            raise _CodeReviewFormatError(
                f"Code Review sha256 mismatch for {file.path}: "
                f"expected {packaged_file.sha256}, got {file.sha256}"
            )
        if file.mode != packaged_file.mode:
            raise _CodeReviewFormatError(
                f"Code Review mode mismatch for {file.path}: "
                f"expected {packaged_file.mode}, got {file.mode}"
            )

    return evaluation


def _failed_code_review_evaluation(intent: Intent, reason: str) -> CodeReviewEvaluation:
    """Build a schema-complete denial when Code Review itself fails."""
    files = [
        CodeReviewFileAssessment(
            path=packaged_file.path,
            sha256=packaged_file.sha256,
            mode=packaged_file.mode,
            review_status="inconclusive",
            matches_claimed_behavior="unclear",
            observed_operations=["Code Review did not complete successfully."],
            risk_findings=[reason],
            evidence=["No validated Code Review JSON result was produced."],
        )
        for packaged_file in intent.packaged_files
    ]
    return CodeReviewEvaluation(
        decision="deny",
        reviewed_all_content=False,
        claim_match="unclear",
        overall_risk="high",
        reasoning=(
            f"{reason} Defaulting to deny because voting members require a "
            "schema-valid Code Review result for packaged-file execution."
        ),
        files=files,
        risk_concerns=["Code review failure"],
        conditions=[],
        guidance="Retry with a valid Code Review JSON result covering every packaged file.",
    )


def format_debate_context(
    member_votes: list[dict[str, Any]],
    current_member_id: str,
    round_number: int,
) -> str:
    """
    Format debate context for a council member.

    Shows all other members' positions from the previous round,
    plus this member's own prior vote for self-reference.

    Parameters
    ----------
    member_votes : List[Dict[str, Any]]
        List of dicts with keys: member_id, display_name,
        decision, reasoning, risk_concerns
    current_member_id : str
        The member receiving this context
    round_number : int
        The debate round number (1-indexed)

    Returns
    -------
    str
        The formatted markdown debate context.
    """
    parts = [
        f"# Council Debate — Round {round_number}",
        "",
        "The council did not reach a unanimous decision. Below are all members' positions",
        "from the previous round. Please reconsider the intent in light of these perspectives.",
        "",
        "## Other Members' Positions",
        "",
    ]

    own_vote = None
    for vote in member_votes:
        if vote["member_id"] == current_member_id:
            own_vote = vote
            continue
        parts.append(
            f"**{vote.get('display_name', vote['member_id'])}**"
            f" — Decision: **{vote['decision'].upper()}**"
        )
        parts.append(f"Reasoning: {vote.get('reasoning', 'No reasoning provided')}")
        concerns = vote.get("risk_concerns", [])
        if concerns:
            parts.append(f"Risk concerns: {', '.join(concerns)}")
        parts.append("")

    if own_vote:
        parts.append("## Your Previous Vote")
        parts.append("")
        parts.append(f"You voted: **{own_vote['decision'].upper()}**")
        parts.append(f"Your reasoning: {own_vote.get('reasoning', '')}")
        parts.append("")

    parts.append("---")
    parts.append("")
    parts.append("You may **change** or **maintain** your vote. Respond with the same JSON format.")
    parts.append(
        "Consider the other members' perspectives carefully, but vote based on your own judgment."
    )

    return "\n".join(parts)


def _strip_markdown_fences(content: str) -> str:
    """Strip markdown code fences from LLM response."""
    content = content.strip()
    if content.startswith("```"):
        # Remove opening fence (```json, ```, etc.)
        first_newline = content.index("\n")
        content = content[first_newline + 1 :]
    if content.endswith("```"):
        content = content[:-3]
    return content.strip()


def _messages_for_attempt(
    base_messages: list[LLMMessage],
    invalid_response: str = "",
) -> list[LLMMessage]:
    """Add a corrective instruction after a malformed structured response."""
    if not invalid_response:
        return base_messages
    return [
        *base_messages,
        LLMMessage(role=LLMRole.ASSISTANT, content=invalid_response[:2000]),
        LLMMessage(
            role=LLMRole.USER,
            content=(
                "Your previous response was not accepted by the JSON parser or did not "
                "meet the required schema. Reply again with exactly one valid JSON "
                "object, no markdown fences, no commentary outside JSON, and all "
                "required fields populated with concrete reasoning."
            ),
        ),
    ]


def _load_json_object(content: str) -> dict[str, Any]:
    """Load the first JSON object from an LLM response."""
    stripped = _strip_markdown_fences(content)
    decoder = json.JSONDecoder()
    candidates = [stripped]

    first_brace = stripped.find("{")
    if first_brace > 0:
        candidates.append(stripped[first_brace:])

    for candidate in candidates:
        try:
            parsed, _ = decoder.raw_decode(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed

    raise json.JSONDecodeError("No JSON object found", stripped, 0)


class _EmptyReasoningError(ValueError):
    """Raised when the LLM returned valid JSON but omitted reasoning."""


def _parse_evaluation(
    content: str,
    *,
    raise_on_parse_error: bool = False,
) -> IntentEvaluation:
    """Parse LLM response into a structured evaluation.

    Raises
    ------
    _EmptyReasoningError
        When the response parses but reasoning is missing/empty. The caller's
    retry loop converts this into a re-ask with the strengthened prompt.
    """
    try:
        data = _load_json_object(content)
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning(
            "Failed to parse LLM evaluation response: %s — raw content: %r",
            e,
            content[:500],
        )
        if raise_on_parse_error:
            raise
        return IntentEvaluation(
            decision="deny",
            reasoning=f"Could not parse LLM response: {content[:200]}",
            risk_concerns=["Unparseable LLM response"],
        )

    decision = str(data.get("decision", "deny")).lower().strip()
    if decision not in ("approve", "deny"):
        decision = "deny"
    reasoning = (data.get("reasoning") or "").strip()
    # Reject sentinel placeholders the model sometimes emits instead of
    # actual reasoning — treat them as equivalent to empty.
    if reasoning.lower() in {"null", "none", "n/a", "na", "see above", "see below"}:
        reasoning = ""
    if not reasoning:
        logger.warning(
            "LLM returned decision=%s with empty/missing reasoning — raw: %r",
            decision,
            content[:500],
        )
        raise _EmptyReasoningError(
            f"LLM returned decision={decision} but omitted reasoning. "
            f"Raw response (first 200 chars): {content[:200]!r}"
        )

    return IntentEvaluation(
        decision=decision,
        reasoning=reasoning,
        risk_concerns=data.get("risk_concerns", []),
        conditions=data.get("conditions", []),
        guidance=str(data.get("guidance", "") or ""),
    )


# ── Chairman ─────────────────────────────────────────────────────────

CHAIRMAN_SYSTEM_PROMPT = """You are the Chairman of a security council
reviewing privileged action requests.

You do NOT vote to approve or deny. Instead, you decide the VOTING THRESHOLD
that the other council members must meet for this specific intent.

Default posture:
- "unanimous": ALL council members must approve. This is the baseline.
- "quorum": A simple majority of council members must approve. This is only
  honored by deployments that explicitly enable quorum in configuration.

Choose based on the risk profile of the action:
- Low-risk, read-only, well-scoped actions may use "quorum" only when quorum is
  enabled in deployment configuration
- Destructive, broad, production, or sensitive actions → "unanimous"
- When in doubt, choose "unanimous"

RESPONSE FORMAT:
Respond with a JSON object:
{
  "threshold": "unanimous",
  "reasoning": "brief explanation of why this threshold",
  "risk_level": "medium"
}

Allowed values:
- threshold: "quorum" or "unanimous"
- risk_level: "low", "medium", "high", or "critical"

Do not choose "unanimous" solely because the request uses Terraform, kubectl, git,
or another powerful tool. Judge the exact subcommand and the supplied evidence.
Plan, show, output, validate, and state-list style operations are lower risk than
apply, destroy, restart, key access, or broad destructive changes.

Return exactly one JSON object and no additional text."""


def _deterministic_threshold_floor(intent: Intent) -> ChairmanDecision | None:
    """Apply a small set of threshold floors for obvious high-risk patterns."""
    command = (intent.command or "").lower()
    action_type = (intent.action_type or "").lower()
    environment = (intent.target_environment or "").lower()
    blast_radius = (intent.blast_radius or "").lower()
    description = (intent.description or "").lower()
    justification = (intent.justification or "").lower()

    if command:
        if references_sensitive_path(
            command,
            working_directory=intent.working_directory,
            target_resource=intent.target_resource,
            sensitive_paths=frozenset({"/app/keys"}),
        ):
            return ChairmanDecision(
                threshold="unanimous",
                risk_level="high",
                reasoning=(
                    "Direct filesystem access to the council key directory should receive"
                    " unanimous review. Even public material in that directory should be"
                    " distributed through an explicit interface rather than ad hoc reads."
                ),
            )

        if re.search(r"/usr/bin/find\s+/app(?:\s|$).*?-delete", command):
            return ChairmanDecision(
                threshold="unanimous",
                risk_level="high",
                reasoning=(
                    "Recursive deletion over the shared /app tree has broader failure modes than a"
                    " normal low-risk cleanup. This kind of shared-tree mutation requires unanimous"
                    " review unless the request proves the tree is disposable build output."
                ),
            )

        if environment == "production" and (
            re.search(r"/bin/chmod\s+\+x\s+\S+", command)
            and ("/scripts/" in command or command.endswith(".sh"))
        ):
            return ChairmanDecision(
                threshold="unanimous",
                risk_level="high",
                reasoning=(
                    "Making a script executable in production enables future code execution from"
                    " that file. Without reviewed content or immutable provenance, this requires"
                    " unanimous review."
                ),
            )

        if (
            action_type in {"file.read", "system.info", "network.request"}
            or "read-only" in blast_radius
            or "read only" in blast_radius
            or "no state change" in blast_radius
            or "no state changes" in blast_radius
            or "no files modified" in blast_radius
            or "read-only" in justification
            or "read only" in justification
            or "read-only" in description
            or "read only" in description
        ) and shell_redirection_writes_to_disk(command):
            return ChairmanDecision(
                threshold="unanimous",
                risk_level="high",
                reasoning=(
                    "The request is framed as read-only or informational, but the command writes"
                    " to disk via shell redirection. Material contradictions between stated and"
                    " actual effects require unanimous review."
                ),
            )

    return None


def _parse_chairman_decision(
    content: str,
    *,
    raise_on_parse_error: bool = False,
) -> ChairmanDecision:
    """Parse the chairman LLM response into a threshold decision."""
    try:
        data = _load_json_object(content)
        return ChairmanDecision(
            threshold=data.get("threshold", "unanimous"),
            reasoning=data.get("reasoning", ""),
            risk_level=data.get("risk_level", "medium"),
        )
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning("Failed to parse chairman response: %s — defaulting to unanimous", e)
        if raise_on_parse_error:
            raise
        return ChairmanDecision(
            threshold="unanimous",
            reasoning=f"Could not parse chairman response: {e}. Defaulting to unanimous.",
            risk_level="high",
        )


class ChairmanAgent:
    """
    The council chairman — decides voting threshold, does not vote.

    The chairman evaluates each intent and decides whether a simple
    majority (quorum) or unanimous approval is required.
    """

    def __init__(
        self,
        backend: LLMBackend,
        system_prompt: str = "",
        *,
        allow_quorum: bool = False,
        temperature: float = 0.0,
        max_tokens: int = DEFAULT_CHAIRMAN_MAX_TOKENS,
        max_retries: int = MAX_RETRIES,
        retry_budget_seconds: float = DEFAULT_RETRY_BUDGET_SECONDS,
        max_retry_after_seconds: float = DEFAULT_MAX_RETRY_AFTER_SECONDS,
    ):
        self._backend = backend
        self._allow_quorum = allow_quorum
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._max_retries = max_retries
        self._retry_budget_seconds = retry_budget_seconds
        self._max_retry_after_seconds = max_retry_after_seconds
        self._system_prompt = system_prompt or CHAIRMAN_SYSTEM_PROMPT

    @property
    def allow_quorum(self) -> bool:
        return self._allow_quorum

    @property
    def provider_name(self) -> str:
        return self._backend.provider_name()

    def _enforce_threshold_policy(self, decision: ChairmanDecision) -> ChairmanDecision:
        """Clamp quorum to unanimous unless deployment config explicitly enables quorum."""
        if self._allow_quorum or decision.threshold != "quorum":
            return decision
        return ChairmanDecision(
            threshold="unanimous",
            risk_level=decision.risk_level,
            reasoning=(
                "Quorum was requested by the chairman model, but this deployment has "
                "not enabled chairman.allow_quorum. Defaulting to unanimous. "
                f"LLM assessment: {decision.reasoning}"
            ),
        )

    async def decide_threshold(self, intent: Intent) -> ChairmanDecision:
        """Decide the voting threshold for this intent. Retries on failure."""
        review_prompt = _format_intent_for_review(intent)
        messages = [
            LLMMessage(role=LLMRole.SYSTEM, content=self._system_prompt),
            LLMMessage(role=LLMRole.USER, content=review_prompt),
        ]

        try:
            decision, response, attempt, call_ms = await _complete_with_retry(
                self._backend,
                messages,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                parse=lambda content: _parse_chairman_decision(content, raise_on_parse_error=True),
                log_ctx=f"Chairman evaluating {intent.intent_id}",
                max_retries=self._max_retries,
                retry_budget_seconds=self._retry_budget_seconds,
                max_retry_after_seconds=self._max_retry_after_seconds,
            )
        except _RetryExhaustedError as e:
            return ChairmanDecision(
                threshold="unanimous",
                reasoning=(
                    f"Chairman evaluation failed after {e.attempts} attempts:"
                    f" {e.last_error}. Defaulting to unanimous."
                ),
                risk_level="high",
            )

        baseline = _deterministic_threshold_floor(intent)
        if baseline and baseline.threshold == "unanimous" and decision.threshold != "unanimous":
            decision = ChairmanDecision(
                threshold=baseline.threshold,
                risk_level=baseline.risk_level,
                reasoning=f"{baseline.reasoning} LLM assessment: {decision.reasoning}",
            )
        decision = self._enforce_threshold_policy(decision)
        logger.info(
            "llm_latency role=chairman provider=%s intent=%s attempt=%d ms=%.1f "
            "tokens=%d threshold=%s risk=%s",
            self._backend.provider_name(),
            intent.intent_id,
            attempt + 1,
            call_ms,
            _token_count(response.usage),
            decision.threshold,
            decision.risk_level,
        )
        return decision

    async def pre_authorize(
        self,
        intent: Intent,
        agent_permissions: set[str],
        required_permissions: set[str],
    ) -> "ChairmanPreAuthDecision":
        """
        Two-phase pre-authorization check.

        Phase 1 (deterministic): Check agent_permissions ⊇ required_permissions.
        Phase 2 (LLM): If permissions pass, evaluate risk and decide threshold.
        """
        # Phase 1 — Permission gate (no LLM call)
        missing = required_permissions - agent_permissions
        if missing:
            return ChairmanPreAuthDecision(
                authorized=False,
                threshold=None,
                risk_level="medium",
                reasoning=f"Agent lacks required permissions: {', '.join(sorted(missing))}",
                missing_permissions=sorted(missing),
            )

        # Phase 2 — LLM-based risk assessment
        decision = await self.decide_threshold(intent)
        return ChairmanPreAuthDecision(
            authorized=True,
            threshold=decision.threshold,
            risk_level=decision.risk_level,
            reasoning=decision.reasoning,
            missing_permissions=[],
        )
