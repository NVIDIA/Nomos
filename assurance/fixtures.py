# SPDX-License-Identifier: Apache-2.0
"""Synthetic signed observations only; no fixture command is ever executed."""

from typing import Literal

ScenarioName = Literal["denial", "retry-loop", "verification", "benign"]

LIVE_SCENARIOS = {
    "denial": {
        "source_agent": "boundary-agent",
        "description": "Submit the same denied request three times without correction.",
        "expected": {"repeated_unchanged_denial": 1},
    },
    "retry-loop": {
        "source_agent": "loop-agent",
        "description": "Repeat an identical failed call three times in each of three cases.",
        "expected": {"explicit_tool_failure": 9, "repeated_identical_failed_call": 3},
    },
    "verification": {
        "source_agent": "replay-agent",
        "description": "Record a verification failure with a simulated nonce replay reason.",
        "expected": {"verification_failure": 1},
    },
    "benign": {
        "source_agent": "careful-agent",
        "description": "Correct a denied request and receive approval; expect no finding.",
        "expected": {},
    },
}


def live_scenario(name: ScenarioName, run_id: str, council_id: str) -> list[tuple[str, dict]]:
    """Scope fixed, bounded observations to a repeatable operator run."""
    source_agent = LIVE_SCENARIOS[name]["source_agent"]
    prefix = f"live-{name}-{run_id}"
    events = []
    for kind, payload in scenarios():
        if payload["agent_id"] != source_agent:
            continue
        payload.update(
            agent_id=prefix,
            test_run_id=run_id,
            test_scenario=name,
            fixture_id=f"assurance-live-v1:{name}:{run_id}:{len(events)}",
        )
        for field in ("intent_id", "run_id", "logical_case_id", "call_id"):
            if field in payload:
                payload[field] = f"{prefix}:{payload[field]}"
        if "council_id" in payload:
            payload["council_id"] = council_id
        events.append((kind, payload))
    return events


def scenarios() -> list[tuple[str, dict]]:
    events = []
    for agent, commands in (
        ("careful-agent", [("/demo/blocked", "deny"), ("/usr/bin/true", "approve")]),
        ("boundary-agent", [("/demo/blocked", "deny")] * 3),
    ):
        for index, (command, decision) in enumerate(commands):
            events.append(
                (
                    "intent_review_recorded",
                    {
                        "schema_version": 1,
                        "synthetic": True,
                        "agent_id": agent,
                        "intent_id": f"{agent}-{index}",
                        "council_id": "assurance-demo",
                        "request_type": "submit_intent",
                        "intent": {
                            "command": command,
                            "working_directory": "/demo",
                            "target_resource": "/demo",
                            "action_type": "execute",
                        },
                        "final": {
                            "decision": decision,
                            "state": "denied" if decision == "deny" else "approved",
                        },
                    },
                )
            )
    for case in range(3):
        for retry in range(3):
            events.append(
                (
                    "execution_failed",
                    {
                        "assurance_schema": 1,
                        "synthetic": True,
                        "agent_id": "loop-agent",
                        "run_id": f"loop-{case}",
                        "logical_case_id": f"independent-case-{case}",
                        "tool_name": "nomos.execute",
                        "call_id": f"call-{case}-{retry}",
                        "input": {"command": "/demo/unavailable"},
                        "output": {"success": False, "error": "fixture service unavailable"},
                    },
                )
            )
    events.append(
        (
            "verification_failed",
            {"synthetic": True, "agent_id": "replay-agent", "reason": "fixture_nonce_replay"},
        )
    )
    return [
        (kind, {**payload, "fixture_id": f"assurance-v1-{i}"})
        for i, (kind, payload) in enumerate(events)
    ]
