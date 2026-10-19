# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""A denial must always tell the caller something they can act on."""

from __future__ import annotations

import json

import pytest
import yaml
from pydantic import TypeAdapter

from nomos.nat.functions import AuthorizeOutput, MemberVote, ProposeIntentOutput

# The production helper itself, not a copy of it. The previous version of this
# file reimplemented the assembly, so a regression in `CouncilService.authorize`
# left these tests green -- which is the failure mode the file exists to prevent.
from nomos.nat.functions import build_denial_guidance as _guidance_for


def test_denial_explains_revision_without_retryable_or_changing_member_guidance():
    member_guidance = ["Check file metadata without reading the credential."]
    result = AuthorizeOutput(status="denied", guidance=member_guidance)
    payload = result.model_dump(mode="json")

    assert "retryable" not in payload
    assert result.retryable is False  # Preserve the Python API default.
    note = payload["guidance"][0]
    assert "retryable" not in note
    assert "user's authorized goal" in note
    assert "require fresh authorization" in note
    assert "Do not invent permissions or justification" in note
    assert "unchanged policy-denied requests" in note
    assert "stop and ask" in note
    assert payload["guidance"][1:] == member_guidance
    assert member_guidance == ["Check file metadata without reading the credential."]
    assert AuthorizeOutput.model_validate_json(result.model_dump_json()).guidance == result.guidance


@pytest.mark.parametrize("status", ["approved", "error"])
def test_retry_clarification_does_not_change_other_authorization_outcomes(status):
    result = AuthorizeOutput(status=status, guidance=["Original guidance."])
    assert result.guidance == ["Original guidance."]


def test_retryable_schema_describes_optional_overload_indicator():
    for schema in (AuthorizeOutput, ProposeIntentOutput):
        for mode in ("validation", "serialization"):
            output_schema = schema.model_json_schema(mode=mode)
            description = output_schema["properties"]["retryable"]["description"]
            assert "unchanged request" in description
            assert "omitted" in description
            assert "retryable" not in output_schema.get("required", [])


@pytest.mark.parametrize("status", ["approved", "denied", "error"])
@pytest.mark.parametrize("explicit_false", [False, True])
def test_authorize_omits_false_retryable_in_all_serialization_paths(status, explicit_false):
    result = AuthorizeOutput(status=status, **({"retryable": False} if explicit_false else {}))
    for payload in (
        result.model_dump(),
        result.model_dump(mode="json"),
        json.loads(result.model_dump_json()),
        TypeAdapter(list[AuthorizeOutput]).dump_python([result])[0],
        json.loads(TypeAdapter(list[AuthorizeOutput]).dump_json([result]))[0],
    ):
        assert "retryable" not in payload
        # Omit only this field, not unrelated default-valued fields.
        assert payload["writ"] is None
        assert payload["error"] == ""
        assert payload["council_members"] == []


@pytest.mark.parametrize("success", [False, True])
def test_propose_omits_false_retryable_without_dropping_other_defaults(success):
    result = ProposeIntentOutput(success=success)
    for payload in (result.model_dump(), json.loads(result.model_dump_json())):
        assert "retryable" not in payload
        assert payload["success"] is success
        assert payload["error"] == ""
        assert payload["requires_approval"] is False


@pytest.mark.parametrize(
    "result",
    [
        AuthorizeOutput(status="error", retryable=True),
        ProposeIntentOutput(success=False, retryable=True),
    ],
)
def test_overload_retryable_survives_serialization_and_round_trip(result):
    assert result.model_dump()["retryable"] is True
    assert json.loads(result.model_dump_json())["retryable"] is True
    restored = type(result).model_validate_json(result.model_dump_json())
    assert restored.retryable is True


def test_retry_clarification_preserves_infrastructure_failure_recovery_advice():
    advice = _guidance_for(
        [
            MemberVote(
                member_id="reviewer",
                decision="deny",
                reasoning="Provider timeout.",
                risk_concerns=["LLM evaluation failure"],
            )
        ]
    )
    result = AuthorizeOutput(status="denied", guidance=advice)
    assert result.retryable is False
    assert result.guidance[1:] == advice
    assert "Retry; if it persists" in result.guidance[1]


def _monitor_upstream_timeout() -> float:
    """Read the read-timeout off the client `build_app()` actually constructs.

    Asserting on the runtime object rather than on the source text: the regex
    this replaced proved only that the literal `httpx.Timeout(180.0)` appeared
    somewhere in the module, which would still hold if the client stopped using
    it.
    """
    from monitor.server.main import build_app

    app = build_app(upstream_url="http://upstream.invalid/mcp")
    timeout = app.state.upstream.timeout
    # httpx.Timeout carries four values; the upstream deadline that matters for
    # a slow council response is the read timeout.
    return float(timeout.read if timeout.read is not None else timeout.connect)


def test_denial_from_member_failure_is_not_silent():
    """The observed bug: every denying member timed out, so guidance came back [].

    A caller then sees `status: denied` with `guidance: []` and `error: ""` -- no
    statement of what went wrong and no remedy, for a failure that is not about
    their request at all.
    """
    votes = [
        MemberVote(member_id="command-integrity", decision="approve"),
        MemberVote(
            member_id="environment-isolation",
            decision="deny",
            reasoning="Evaluation failed after 1 attempts via nim: provider timeout.",
            risk_concerns=["LLM evaluation failure"],
        ),
    ]

    guidance = _guidance_for(votes)

    assert guidance, "a denial caused by member failure must still carry guidance"
    text = guidance[0]
    assert "environment-isolation" in text, "name the member that could not be evaluated"
    assert "not because of anything in the request" in text
    assert "request_timeout_seconds" in text, "point at the setting that governs this"


def test_a_mixed_denial_is_not_reported_as_infrastructure_only():
    """A member can deny on the merits AND leave `guidance` empty.

    Reaching the fallback proves only that no vote supplied guidance and that at
    least one denying vote failed to evaluate. Claiming the denial was "not
    because of anything in the request" in that mixed case sends the caller to
    check model availability while their request is the actual problem.
    """
    votes = [
        MemberVote(
            member_id="blast-containment",
            decision="deny",
            reasoning="Blast radius is understated for a production namespace.",
            risk_concerns=["Blast radius understated"],
        ),
        MemberVote(
            member_id="environment-isolation",
            decision="deny",
            reasoning="Evaluation failed after 2 attempts via nim: provider timeout.",
            risk_concerns=["LLM evaluation failure"],
        ),
    ]

    text = _guidance_for(votes)[0]

    assert "not because of anything in the request" not in text, (
        "a substantive denial was present, so the denial was partly about the request"
    )
    assert "blast-containment" in text, "name the member that objected on the merits"
    assert "environment-isolation" in text, "name the member that could not be evaluated"
    assert "retrying alone will not clear this" in text, (
        "a retry cannot clear a substantive objection; say so"
    )


def test_a_wholly_unevaluated_denial_still_says_so_plainly():
    """The strong wording remains correct when every denial IS infrastructure."""
    votes = [
        MemberVote(member_id="command-integrity", decision="approve"),
        MemberVote(
            member_id="environment-isolation",
            decision="deny",
            reasoning="Evaluation failed after 2 attempts via nim: provider timeout.",
            risk_concerns=["LLM evaluation failure"],
        ),
    ]

    text = _guidance_for(votes)[0]

    assert "not because of anything in the request" in text
    assert "request_timeout_seconds" in text


def test_substantive_guidance_is_never_replaced_by_the_fallback():
    """A real objection must win: the fallback is only for the empty case."""
    votes = [
        MemberVote(
            member_id="blast-containment",
            decision="deny",
            guidance="State the blast radius and a rollback step.",
            risk_concerns=["Blast radius is missing"],
        ),
    ]

    assert _guidance_for(votes) == ["State the blast radius and a rollback step."]


def test_shipped_council_cap_matches_the_parallel_peak():
    """The endpoint cap must cover one authorize's PARALLEL peak -- and no more.

    The peak is the chairman plus the members, which run concurrently. Code
    review is NOT part of it: `_run_code_review` is awaited before those tasks
    are created, so it never overlaps them. An earlier version of this test
    counted five sequential roles and required a cap of 5+, which justified
    raising the shared endpoint semaphore to 8 -- enough for two authorize
    fan-outs to hit the provider at once, raising 429s without raising the
    provider's quota.

    tests/test_nat_functions.py measures the peak against a real authorize;
    this one only checks the shipped configuration against it.
    """
    from pathlib import Path

    config = yaml.safe_load(Path("configs/council.yaml").read_text())
    backends = config["llm_backends"]

    enabled_members = [m for m in config["members"] if m.get("enabled", True)]
    parallel_peak = 1 + len(enabled_members)  # chairman + members

    caps = {name: b.get("max_concurrent", 4) for name, b in backends.items()}
    assert min(caps.values()) >= parallel_peak, (
        f"caps {caps} are below the parallel peak of {parallel_peak} "
        f"({len(enabled_members)} members + chairman), so a call queues every time"
    )

    # The limiter is shared per (provider, base_url) and the FIRST registration
    # wins -- a differing value elsewhere is ignored with a warning, leaving a
    # config that reads one way and behaves another.
    by_endpoint: dict[tuple[str, str], set[int]] = {}
    for name, b in backends.items():
        by_endpoint.setdefault((b["provider"], b.get("base_url", "")), set()).add(caps[name])
    for endpoint, values in by_endpoint.items():
        assert len(values) == 1, (
            f"backends on {endpoint} declare conflicting max_concurrent {values}; "
            "the first registration wins and the rest are silently ignored"
        )


def test_timeout_layers_are_ordered_so_each_can_do_its_job():
    """The three deadlines must nest, or the outer one cancels the inner.

    Measured on the API Catalog with a real council prompt, these models return in
    10-47s with wide variance. At the shipped 30s per-call timeout and 40s budget,
    members intermittently timed out and the council denied for an infrastructure
    reason rather than a judgement about the request.
    """
    from pathlib import Path

    config = yaml.safe_load(Path("configs/council.yaml").read_text())
    timeouts = {name: b["request_timeout_seconds"] for name, b in config["llm_backends"].items()}
    budget = config["llm_retry_budget_seconds"]

    # Two different questions, two different aggregates. The floor asks whether
    # EVERY backend allows for the observed tail, so the binding case is the
    # smallest. The budget invariant must hold for every backend too, but there
    # the binding case is the LARGEST -- taking the minimum let a backend whose
    # timeout exceeded the budget pass unnoticed.
    assert min(timeouts.values()) >= 50, (
        f"per-call timeouts {timeouts} include one below the observed tail"
    )
    for name, per_call in timeouts.items():
        assert budget > per_call, (
            f"retry budget {budget}s does not exceed one call to {name} ({per_call}s), "
            "so a single slow call exhausts it and the retry never runs"
        )

    shed = min(
        b.get("overload_latency_threshold_seconds", 120) for b in config["llm_backends"].values()
    )
    assert shed > budget, (
        f"load-shedding threshold {shed}s is not above the retry budget {budget}s, so "
        "the council sheds requests it has the budget to complete "
        "(observed: 'council_overloaded: average intent response time ~181s exceeds "
        "the latency threshold (120s)')"
    )

    # Build the real app and read the timeout off the client it actually
    # created. A regex over the source proves the literal is present; it does
    # not prove build_app installs it, nor that the forwarding path uses the
    # client default -- and those are the properties this invariant needs.
    proxy = _monitor_upstream_timeout()
    assert proxy > budget, (
        f"monitor proxy timeout {proxy}s is not above the council budget {budget}s, "
        "so the proxy abandons requests the council would have answered"
    )

    # The outermost layer is the CLIENT the documentation tells people to write.
    # `streamablehttp_client` defaults to 30s, well under every deadline below
    # it, so a quick-start copied verbatim hangs up before a healthy council
    # answers -- measured responses reach 47s.
    import re

    readme = Path("README.md").read_text()
    documented = [float(m) for m in re.findall(r"timeout=(\d+(?:\.\d+)?)", readme)]
    assert documented, "no explicit client timeout in the README examples"
    for client_timeout in documented:
        assert client_timeout > proxy, (
            f"a documented client timeout of {client_timeout}s is not above the monitor "
            f"proxy deadline {proxy}s, so the example disconnects before the "
            "deployment it is demonstrating can reply"
        )


class TestTimeoutIsRetried:
    """A timeout must be retried, not treated as a verdict.

    A council evaluation has no side effects, so re-issuing it is safe. Before
    this, `LLMTimeoutError` short-circuited the retry loop with an explicit "not
    retrying", so one slow response permanently denied the request even though
    `llm_max_retries` was configured and the wall-clock budget had room. Against a
    hosted endpoint, member latency varied from ~5s to past the per-call timeout on
    identical requests, so the first attempt timing out says little about the next.
    """

    def test_timeout_branch_no_longer_short_circuits(self):
        from pathlib import Path

        source = Path("nomos/llm/council_agent.py").read_text()
        timeout_branch = source.split("except LLMTimeoutError as e:", 1)[1]
        timeout_branch = timeout_branch.split("except Exception as e:", 1)[0]

        assert "not retrying" not in timeout_branch, "the timeout branch still refuses to retry"
        assert "continue" in timeout_branch, "the timeout branch must re-enter the loop"
        assert "max_retries" in timeout_branch, "retries must still be bounded"
        assert "retry_budget_seconds" in timeout_branch, (
            "the wall-clock budget must still bound the retry"
        )

    @pytest.mark.asyncio
    async def test_a_timeout_then_a_success_returns_the_success(self):
        """The behaviour that matters: a transient timeout must not become a denial."""
        from nomos.llm.base import LLMMessage, LLMResponse, LLMRole, LLMTimeoutError
        from nomos.llm.council_agent import _complete_with_retry

        calls = {"n": 0}

        class _Backend:
            def provider_name(self):
                return "test"

            async def complete(self, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise LLMTimeoutError("Request timed out.")
                return LLMResponse(content='{"ok": true}')

        parsed, _response, attempt, _ms = await _complete_with_retry(
            _Backend(),
            [LLMMessage(role=LLMRole.USER, content="x")],
            temperature=0.0,
            max_tokens=16,
            parse=lambda c: c,
            log_ctx="test",
            max_retries=2,
            retry_budget_seconds=60.0,
            max_retry_after_seconds=10.0,
        )

        assert calls["n"] == 2, "the timeout should have been retried exactly once"
        assert parsed == '{"ok": true}'
        assert attempt == 1, "the successful attempt should be the second one"
