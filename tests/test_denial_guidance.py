# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""A denial must always tell the caller something they can act on."""

from __future__ import annotations

import yaml

from nomos.nat.functions import MemberVote


def _guidance_for(votes: list[MemberVote]) -> list[str]:
    """Mirror the guidance assembly in the authorize path."""
    parts = [v.guidance for v in votes if v.guidance]
    if not parts:
        unevaluated = [
            v
            for v in votes
            if v.decision == "deny"
            and any("evaluation failure" in c.lower() for c in v.risk_concerns)
        ]
        if unevaluated:
            names = ", ".join(v.member_id for v in unevaluated if v.member_id)
            parts = [
                f"Denied because {names or 'a council member'} could not be "
                "evaluated, not because of anything in the request. The council "
                "fails closed when a member is unreachable. Retry; if it persists, "
                "check the configured model's availability and "
                "request_timeout_seconds. Member detail: "
                + "; ".join(v.reasoning for v in unevaluated if v.reasoning)
            ]
    return parts


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


def test_shipped_council_sizes_concurrency_to_one_authorize():
    """Five calls per authorize must not queue behind a cap of four.

    A queued call's wait counts against `request_timeout_seconds`, which is how
    members timed out and denials arrived with no guidance.
    """
    config = yaml.safe_load(open("configs/council.yaml"))
    backends = config["llm_backends"]

    calls = [m["backend"] for m in config["members"]]
    for role in ("chairman", "code_review"):
        backend = (config.get(role) or {}).get("backend")
        if backend:
            calls.append(backend)

    # The cap is shared by every backend on the same endpoint, so compare the
    # total call count against the smallest configured cap.
    caps = [b.get("max_concurrent", 4) for b in backends.values()]
    assert min(caps) >= len(calls), (
        f"{len(calls)} calls per authorize against a shared cap of {min(caps)}"
    )
