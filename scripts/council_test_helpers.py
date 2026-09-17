# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


"""Shared helpers for Nomos council test scripts."""

import json
import os
import textwrap

MCP_URL = os.environ.get(
    "NOMOS_MCP_URL",
    os.environ.get("NOMOS_COUNCIL_URL", "http://localhost:9901/mcp"),
)


def runtime_agent_id(default_agent_id: str) -> str:
    """Return the configured agent id for live council probe scripts."""
    return os.environ.get("NOMOS_AGENT_ID", default_agent_id)


def with_council_auth(args: dict, default_agent_id: str) -> dict:
    """Attach standard live-council auth fields when provided by the environment."""
    request = dict(args)
    request["agent_id"] = runtime_agent_id(default_agent_id)

    auth_token = os.environ.get("NOMOS_AUTH_TOKEN", "")
    if auth_token:
        request["auth_token"] = auth_token

    council_id = os.environ.get("NOMOS_COUNCIL_ID", "")
    if council_id:
        request["council_id"] = council_id

    return request


def print_client_config(default_agent_id: str) -> None:
    """Print non-secret runtime connection details for probe scripts."""
    print(f"Endpoint: {MCP_URL}")
    print(f"Agent: {runtime_agent_id(default_agent_id)}")
    print(f"Auth token: {'set' if os.environ.get('NOMOS_AUTH_TOKEN') else 'not set'}")
    council_id = os.environ.get("NOMOS_COUNCIL_ID", "")
    if council_id:
        print(f"Council: {council_id}")
    print()


def parse_result(tool_result) -> dict:
    if tool_result.content:
        for item in tool_result.content:
            text = getattr(item, "text", None) or str(item)
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass
    return {}


def print_case(idx: int, case: dict, args: dict, result: dict):
    state = result.get("state", "unknown")
    chairman = result.get("chairman", {})
    votes = result.get("council_members", [])

    verdict = (
        "\033[92m✅ DENIED\033[0m" if state == "denied" else f"\033[91m❌ PASSED ({state})\033[0m"
    )

    print(f"\n{'=' * 72}")
    print(f"  CASE {idx}: {case['label']}")
    print(f"{'=' * 72}")

    # ── Full agent request ────────────────────────────────────────────────
    print(f"  {'─' * 68}")
    print("  AGENT REQUEST")
    print(f"  {'─' * 68}")
    print(f"  agent_id         : {args.get('agent_id', '')}")
    print(f"  action_type      : {args.get('action_type', '')}")
    print(f"  target_env       : {args.get('target_environment', '')}")
    print(f"  target_resource  : {args.get('target_resource', '')}")
    print(f"  working_dir      : {args.get('working_directory', '')}")
    print(f"  command_type     : {args.get('command_type', '')}")

    cmd = args.get("command", "")
    cmd_lines = cmd.splitlines()
    if len(cmd_lines) == 1:
        print(f"  command          : {cmd}")
    else:
        print("  command          :")
        for line in cmd_lines:
            print(f"      {line}")

    desc = args.get("description", "")
    print(f"  description      : {textwrap.fill(desc, width=66, subsequent_indent=' ' * 21)}")

    just = args.get("justification", "")
    print(f"  justification    : {textwrap.fill(just, width=66, subsequent_indent=' ' * 21)}")

    br = args.get("blast_radius", "")
    print(f"  blast_radius     : {textwrap.fill(br, width=66, subsequent_indent=' ' * 21)}")

    ctx = args.get("context", "")
    if ctx:
        print(f"  context          : {textwrap.fill(ctx, width=66, subsequent_indent=' ' * 21)}")

    # ── Council verdict ───────────────────────────────────────────────────
    print(f"  {'─' * 68}")
    print("  COUNCIL VERDICT")
    print(f"  {'─' * 68}")
    print(f"  Verdict : {verdict}  |  state={state}")
    if chairman:
        print(
            f"  Chairman: risk={chairman.get('risk_level', '?')}  threshold={chairman.get('threshold', '?')}"
        )
        reasoning = chairman.get("reasoning", "")
        if reasoning:
            wrapped = textwrap.fill(
                reasoning, width=68, initial_indent="            ", subsequent_indent="            "
            )
            print(wrapped)

    debate_history = result.get("debate_history", [])
    if debate_history:
        for rnd in debate_history:
            rnum = rnd.get("round_number", 0)
            rvotes = rnd.get("votes", [])
            if rnum == 0:
                label = "  INITIAL VOTES"
            else:
                label = f"  DEBATE ROUND {rnum}"
            print(f"  {'─' * 68}")
            print(f"{label}")
            print(f"  {'─' * 68}")
            for v in rvotes:
                icon = "\033[92m✓\033[0m" if v.get("decision") == "approve" else "\033[91m✗\033[0m"
                changed = " \033[93m[changed vote]\033[0m" if v.get("changed_vote") else ""
                print(
                    f"  [{icon}] {v.get('member_id', '?'):25s} {v.get('decision', '?').upper()}{changed}"
                )
                reasoning = v.get("reasoning", "")
                if reasoning:
                    print(f"       {textwrap.shorten(reasoning, 120)}")
                for c in v.get("risk_concerns", [])[:5]:
                    print(f"       ⚠  {c}")
    else:
        # Fallback: no debate history, render final votes directly
        print(f"  {'─' * 68}")
        print("  VOTES")
        print(f"  {'─' * 68}")
        for v in votes:
            icon = "\033[92m✓\033[0m" if v.get("decision") == "approve" else "\033[91m✗\033[0m"
            print(f"  [{icon}] {v.get('member_id', '?'):25s} {v.get('decision', '?').upper()}")
            reasoning = v.get("reasoning", "")
            if reasoning:
                print(f"       {textwrap.shorten(reasoning, 120)}")
            for c in v.get("risk_concerns", [])[:5]:
                print(f"       ⚠  {c}")

    if result.get("error"):
        print(f"  ERROR: {result['error']}")


def admission_rejected(result: dict) -> bool:
    """True when the council refused the intent at admission (before review).

    A blocked attack can end as ``state == "denied"`` (reviewed and denied) or
    as an explicit validation rejection (``success`` false, error starting
    ``Invalid intent``). Transport or server errors are neither.
    """
    return result.get("success") is False and str(result.get("error", "")).startswith(
        "Invalid intent"
    )


def blocked(result: dict) -> bool:
    """True when the intent was denied by review or rejected at admission."""
    return result.get("state") == "denied" or admission_rejected(result)


def print_summary(suite_name: str, results: list):
    import sys

    print(f"\n{'=' * 72}")
    print(f"  SUMMARY — {suite_name}")
    print(f"{'=' * 72}")
    for r in results:
        icon = "\033[92m✅\033[0m" if not r["passed_unexpectedly"] else "\033[91m❌\033[0m"
        print(f"  {icon}  {r['label']:60s}  {r['state']}")
    print()
    bad = [r for r in results if r["passed_unexpectedly"]]
    if bad:
        print(f"\033[91m⚠️  {len(bad)} case(s) NOT denied — review council configuration!\033[0m")
        for b in bad:
            print(f"     - {b['label']}  ({b['state']})")
        sys.exit(1)
    else:
        print(f"\033[92m✅  All {len(results)} cases correctly denied.\033[0m")
