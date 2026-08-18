#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""S6 manual validation ladder — L5..L8, run clean-room against a deployed target.

Every rung asserts *why* something was refused, not merely that it was. A negative
security test that accepts any failure passes when the request never reached the
control under test, which is how this branch's replay and forgery checks once
"passed" on a missing header.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import os
import re
import shlex
import sys
import urllib.error
import urllib.request

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from mcp import ClientSession

try:  # mcp >= 1.14 renamed the transport; the old name still works but warns.
    from mcp.client.streamable_http import streamable_http_client as _http_client
except ImportError:  # pragma: no cover - older mcp
    from mcp.client.streamable_http import streamablehttp_client as _http_client

REQUIRED = ("LADDER_MCP_URL", "LADDER_EXEC_URL", "NOMOS_AGENT_ID", "NOMOS_AUTH_TOKEN")
# Interpolated into a shell command, so constrain it rather than trusting it.
WORKSPACE_RE = re.compile(r"^/[A-Za-z0-9._\-/]*$")

results: list[tuple[str, bool, str]] = []


class ConfigError(RuntimeError):
    """Configuration is missing or unusable; the operator can fix it from the text."""


def load_config() -> dict[str, str]:
    missing = [name for name in REQUIRED if not os.environ.get(name)]
    if missing:
        raise ConfigError(
            "missing required environment: "
            + ", ".join(missing)
            + "\n  LADDER_MCP_URL   the council or monitor /mcp endpoint"
            + "\n  LADDER_EXEC_URL  the executor base URL"
            + "\n  NOMOS_AGENT_ID / NOMOS_AUTH_TOKEN  from deploy/.env"
            + "\n  optional: LADDER_PROXY_TOKEN, LADDER_WORKSPACE, LADDER_COUNCIL_ID"
        )
    workspace = os.environ.get("LADDER_WORKSPACE", "/workspace")
    if not WORKSPACE_RE.fullmatch(workspace):
        raise ConfigError(
            f"LADDER_WORKSPACE must be an absolute path of [A-Za-z0-9._-/]; got {workspace!r}. "
            "It is interpolated into the command the council is asked to authorize."
        )
    return {
        "mcp_url": os.environ["LADDER_MCP_URL"],
        "exec_url": os.environ["LADDER_EXEC_URL"],
        "agent_id": os.environ["NOMOS_AGENT_ID"],
        "auth_token": os.environ["NOMOS_AUTH_TOKEN"],
        "proxy_token": os.environ.get("LADDER_PROXY_TOKEN", ""),
        "workspace": workspace,
        "council_id": os.environ.get("LADDER_COUNCIL_ID", "council-prod"),
    }


def record(name: str, passed: bool, detail: str) -> None:
    results.append((name, passed, detail))
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}: {detail}", flush=True)


def submit_writ(cfg: dict[str, str], blob: str) -> dict:
    """Submit a writ, converting transport failures into a recordable result.

    An unreachable executor is a ladder failure, not a traceback: raising here
    aborts the run and loses every rung after it, including the ones that would
    have shown what still works.
    """
    req = urllib.request.Request(  # noqa: S310
        f"{cfg['exec_url']}/execute",
        data=json.dumps({"writ": blob}).encode(),
        headers={"Content-Type": "application/json", "X-Agent-ID": cfg["agent_id"]},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return {"success": False, "error": exc.read().decode()[:400], "http_status": exc.code}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"success": False, "error": f"transport failure: {exc}", "transport_error": True}
    except json.JSONDecodeError as exc:
        return {"success": False, "error": f"executor returned non-JSON: {exc}"}


def refused_because(result: dict, *needles: str) -> tuple[bool, str]:
    """A refusal only counts when it names the control that did the refusing."""
    reason = str(result.get("error", ""))
    if result.get("success"):
        return False, f"NOT refused (success=True) reason={reason[:120]}"
    if result.get("transport_error"):
        return False, f"refused by transport, not by the control under test: {reason[:120]}"
    matched = any(n.lower() in reason.lower() for n in needles)
    if not matched:
        return False, f"refused for the wrong reason: {reason[:160]}"
    return True, f"reason={reason[:140]}"


async def call(session: ClientSession, tool: str, args: dict) -> dict:
    res = await session.call_tool(tool, args)
    text = res.content[0].text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text}


async def run_ladder(cfg: dict[str, str]) -> int:
    headers = {"Authorization": f"Bearer {cfg['proxy_token']}"} if cfg["proxy_token"] else None
    workspace = cfg["workspace"]
    async with _http_client(cfg["mcp_url"], headers=headers, timeout=300) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            tool_names = [t.name for t in (await session.list_tools()).tools]
            print(f"  tools exposed via proxy: {tool_names}", flush=True)

            # L5 — authorize -> writ -> execute
            marker = f"nomos-qa-ladder-{os.urandom(4).hex()}"
            target = f"{workspace}/{marker}.txt"
            command = f"/bin/echo {shlex.quote(marker)} > {shlex.quote(target)}"
            auth = await call(
                session,
                "council_authorize",
                {
                    "auth_token": cfg["auth_token"],
                    "agent_id": cfg["agent_id"],
                    "command": command,
                    "working_directory": workspace,
                    "action_type": "system.exec",
                    "description": (
                        f"Write one short literal line to exactly one new file, {target}, "
                        f"using an absolute executable path. Blast radius: exactly that "
                        f"single file. The filename embeds a fresh random suffix generated "
                        f"for this run, so no pre-existing file can be overwritten or "
                        f"truncated; nothing else in {workspace} is read, modified, or "
                        f"deleted. No network access, no privilege change, no recursion, "
                        f"no globbing."
                    ),
                    "target_resource": target,
                    "justification": (
                        "Automated QA end-to-end check that the council -> signed writ -> "
                        "sandboxed executor path completes and the writ verifies. "
                        f"Rollback: remove exactly {target} if it was created. Since the "
                        "name is unique to this run, that rollback is complete and cannot "
                        "affect any other file."
                    ),
                },
            )
            writ = auth.get("writ")
            record(
                "L5a authorize mints writ",
                bool(writ),
                f"status={auth.get('status')} writ_id={auth.get('writ_id')}",
            )
            if not writ:
                print(json.dumps(auth, indent=2)[:1500])
                return 1

            r1 = submit_writ(cfg, writ)
            record(
                "L5b writ verifies and executes",
                bool(r1.get("success")) and r1.get("exit_code") == 0,
                f"success={r1.get('success')} exit_code={r1.get('exit_code')} "
                f"err={str(r1.get('error'))[:120]}",
            )

            # L6 — replay. Must be refused by the nonce check specifically; a
            # rejection for a bad header or an unreachable executor proves nothing.
            ok, detail = refused_because(submit_writ(cfg, writ), "nonce", "replay")
            record("L6 replay rejected at the nonce check", ok, detail)

            # L7 — forged writ: the real writ's exact shape with a fresh id/nonce,
            # re-signed with an attacker key. Structure and agent binding stay valid,
            # so only the signature check can reject it -- and must be what does.
            k = Ed25519PrivateKey.generate()
            forged = json.loads(base64.b64decode(writ))
            forged["writ_id"] = f"writ-forged-{os.urandom(8).hex()}"
            if "nonce" in forged:
                forged["nonce"] = os.urandom(16).hex()
            sig_field = "signature" if "signature" in forged else None
            body = {k2: v for k2, v in forged.items() if k2 != sig_field}
            canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
            if sig_field:
                forged[sig_field] = k.sign(canonical).hex()
            forged_blob = base64.b64encode(json.dumps(forged).encode()).decode()
            ok, detail = refused_because(submit_writ(cfg, forged_blob), "signature")
            record("L7 forged writ rejected at the signature check", ok, detail)

            # L8 — walk the hash chain. council_ledger_page returns entries
            # newest-first with previous_hash/content_hash, so each entry must name
            # its predecessor, and the page must actually start at the head.
            led = await call(
                session,
                "council_ledger_page",
                {"council_id": cfg["council_id"], "limit": 200},
            )
            entries = led.get("entries", [])
            if not entries:
                record("L8 ledger chain valid", False, f"no entries returned: {str(led)[:200]}")
            else:
                broken = [
                    newer.get("sequence")
                    for newer, older in itertools.pairwise(entries)
                    if newer.get("previous_hash") != older.get("content_hash")
                ]
                seqs = [e.get("sequence") for e in entries]
                contiguous = seqs == list(range(seqs[0], seqs[0] - len(seqs), -1))
                head = led.get("head_sequence")
                # A page that silently starts below the head would hide the most
                # recent entries -- including the ones this run just wrote.
                at_head = head is not None and seqs[0] == head
                record(
                    "L8 ledger chain valid",
                    not broken and contiguous and at_head,
                    f"entries={len(entries)} head={head} first={seqs[0]} "
                    f"at_head={at_head} contiguous={contiguous} broken_links={broken}",
                )

    failed = [n for n, ok, _ in results if not ok]
    print(f"\nLADDER: {len(results) - len(failed)}/{len(results)} passed", flush=True)
    return 1 if failed else 0


async def main() -> int:
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"ladder_probe: {exc}", file=sys.stderr)
        return 2
    return await run_ladder(cfg)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
