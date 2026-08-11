#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""S6 manual validation ladder — L5..L8, run clean-room against a deployed target."""

import asyncio
import base64
import itertools
import json
import os
import sys
import urllib.request

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MCP_URL = os.environ["LADDER_MCP_URL"]
EXEC_URL = os.environ["LADDER_EXEC_URL"]
PROXY_TOKEN = os.environ.get("LADDER_PROXY_TOKEN", "")
AGENT_ID = os.environ["NOMOS_AGENT_ID"]
AUTH_TOKEN = os.environ["NOMOS_AUTH_TOKEN"]
WORKSPACE = os.environ.get("LADDER_WORKSPACE", "/workspace")

results: list[tuple[str, bool, str]] = []


def record(name: str, passed: bool, detail: str) -> None:
    results.append((name, passed, detail))
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}: {detail}", flush=True)


def submit_writ(blob: str, agent_id: str = AGENT_ID) -> dict:
    req = urllib.request.Request(  # noqa: S310
        f"{EXEC_URL}/execute",
        data=json.dumps({"writ": blob}).encode(),
        headers={"Content-Type": "application/json", "X-Agent-ID": agent_id},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return {"success": False, "http_status": exc.code, "error": exc.read().decode()[:400]}


async def call(session: ClientSession, tool: str, args: dict) -> dict:
    res = await session.call_tool(tool, args)
    text = res.content[0].text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text}


async def main() -> int:
    headers = {"Authorization": f"Bearer {PROXY_TOKEN}"} if PROXY_TOKEN else None
    async with streamablehttp_client(MCP_URL, headers=headers, timeout=300) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            tool_names = [t.name for t in (await session.list_tools()).tools]
            print(f"  tools exposed via proxy: {tool_names}", flush=True)

            # L5 — authorize -> writ -> execute
            marker = f"nomos-qa-ladder-{os.urandom(4).hex()}"
            auth = await call(
                session,
                "council_authorize",
                {
                    "auth_token": AUTH_TOKEN,
                    "agent_id": AGENT_ID,
                    "command": f"/bin/echo {marker} > {WORKSPACE}/{marker}.txt",
                    "working_directory": WORKSPACE,
                    "action_type": "system.exec",
                    "description": (
                        f"Write one short literal line to exactly one new file, "
                        f"{WORKSPACE}/{marker}.txt, using an absolute executable path. "
                        f"Blast radius: exactly that single file. The filename embeds a "
                        f"fresh random suffix generated for this run, so no pre-existing "
                        f"file can be overwritten or truncated; nothing else in "
                        f"{WORKSPACE} is read, modified, or deleted. No network access, "
                        f"no privilege change, no recursion, no globbing."
                    ),
                    "target_resource": f"{WORKSPACE}/{marker}.txt",
                    "justification": (
                        "Automated QA end-to-end check that the council -> signed writ -> "
                        "sandboxed executor path completes and the writ verifies. "
                        f"Rollback: remove exactly {WORKSPACE}/{marker}.txt if it was "
                        "created. Since the name is unique to this run, that rollback is "
                        "complete and cannot affect any other file."
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

            r1 = submit_writ(writ)
            record(
                "L5b writ verifies and executes",
                bool(r1.get("success")) and r1.get("exit_code") == 0,
                f"success={r1.get('success')} exit_code={r1.get('exit_code')} "
                f"err={str(r1.get('error'))[:120]}",
            )

            # L6 — replay
            r2 = submit_writ(writ)
            record(
                "L6 replay rejected",
                not r2.get("success"),
                f"success={r2.get('success')} reason={str(r2.get('error'))[:140]}",
            )

            # L7 — forged writ: take the real writ's exact shape, give it a fresh
            # id/nonce, and re-sign with an attacker key. Structure and agent binding
            # are therefore valid, so only the signature check can reject it.
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
            r3 = submit_writ(base64.b64encode(json.dumps(forged).encode()).decode())
            record(
                "L7 forged writ rejected",
                not r3.get("success"),
                f"success={r3.get('success')} reason={str(r3.get('error'))[:140]}",
            )

            # L8 — ledger chain validity
            # L8 — walk the hash chain. council_ledger_page returns entries newest-first
            # with previous_hash/content_hash, so each entry must name its predecessor.
            led = await call(
                session,
                "council_ledger_page",
                {"council_id": os.environ.get("LADDER_COUNCIL_ID", "council-prod"), "limit": 200},
            )
            entries = led.get("entries", [])
            if not entries:
                record("L8 ledger chain valid", False, f"no entries returned: {str(led)[:200]}")
            else:
                broken = []
                for newer, older in itertools.pairwise(entries):
                    if newer.get("previous_hash") != older.get("content_hash"):
                        broken.append(newer.get("sequence"))
                seqs = [e.get("sequence") for e in entries]
                contiguous = seqs == list(range(seqs[0], seqs[0] - len(seqs), -1))
                record(
                    "L8 ledger chain valid",
                    not broken and contiguous,
                    f"entries={len(entries)} head={led.get('head_sequence')} "
                    f"contiguous={contiguous} broken_links={broken}",
                )

    failed = [n for n, ok, _ in results if not ok]
    print(f"\nLADDER: {len(results) - len(failed)}/{len(results)} passed", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
