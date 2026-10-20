#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Exercise a live council, executor, and signed ledger with harmless real commands.

Only /usr/bin/true and /usr/bin/false can reach the executor. Three bare `true`
proposals exercise strict-shell OPA denial without execution. No request is
automatically retried: an interrupted execution requires operator reconciliation.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values

from assurance.council_source import MAX_RESPONSE_BYTES, PROTOCOL_VERSION, _rpc_result
from assurance.monitor import verify_batch
from scripts.assurance_smoke import analysis_wait_seconds
from trusted_executor_contract import ExecutionResult, WritOfExecution

COMMANDS = {"/usr/bin/true", "/usr/bin/false"}


@dataclass
class Config:
    mcp_url: str
    executor_url: str
    agent_id: str
    auth_token: str = field(repr=False)
    proxy_token: str = field(repr=False)
    council_id: str
    working_directory: str
    council_key_hex: str
    executor_keys: dict[str, list[str]]
    timeout_seconds: float = 240


def load_config(env_file: Path, trust_file: Path) -> Config:
    values = dotenv_values(env_file, interpolate=False)
    trust = json.loads(trust_file.read_text())["trust"]
    if trust["valid_until"] <= time.time():
        raise ValueError("Public-key trust configuration has expired")
    config = Config(
        mcp_url=values["NOMOS_MCP_URL"],
        executor_url=values["NOMOS_EXECUTOR_URL"],
        agent_id=values["NOMOS_AGENT_ID"],
        auth_token=values["NOMOS_AUTH_TOKEN"],
        proxy_token=values["MONITOR_MCP_PROXY_TOKEN"],
        council_id=values["NOMOS_COUNCIL_ID"],
        working_directory=values.get("NOMOS_WORKING_DIRECTORY", "/workspace"),
        council_key_hex=trust["council_public_key_hex"],
        executor_keys=trust["executors"],
    )
    if not all((config.agent_id, config.auth_token, config.council_id, config.proxy_token)):
        raise ValueError("Agent and proxy credentials are required")
    for url in (config.mcp_url, config.executor_url):
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Service URLs must be HTTP(S) without embedded credentials")
    if len(bytes.fromhex(config.council_key_hex)) != 32 or not config.executor_keys:
        raise ValueError("Pinned council and executor public keys are required")
    return config


class Council:
    def __init__(self, config: Config, client: httpx.AsyncClient):
        self.config, self.client = config, client

    async def _rpc(self, message: dict) -> dict:
        async with self.client.stream(
            "POST",
            self.config.mcp_url,
            json={"jsonrpc": "2.0", **message},
            headers={
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": PROTOCOL_VERSION,
                "Authorization": f"Bearer {self.config.proxy_token}",
            },
            timeout=self.config.timeout_seconds,
            follow_redirects=False,
        ) as response:
            response.raise_for_status()
            if response.headers.get("mcp-session-id"):
                raise ValueError("Live validation requires Nomos's stateless MCP worker")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise ValueError("Council response exceeds 8 MiB")
            return (
                _rpc_result(data, response.headers.get("content-type", ""), message["id"])
                if "id" in message
                else {}
            )

    async def call(self, tool: str, arguments: dict) -> dict:
        if tool not in {"council_propose_intent", "council_authorize", "council_ledger_page"}:
            raise ValueError("Tool is outside the live validation contract")
        async with asyncio.timeout(self.config.timeout_seconds):
            initialized = await self._rpc(
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "nomos-assurance-live-validation", "version": "1"},
                    },
                }
            )
            if initialized.get("protocolVersion") != PROTOCOL_VERSION:
                raise ValueError("Unsupported MCP protocol")
            await self._rpc({"method": "notifications/initialized"})
            result = await self._rpc(
                {"id": 2, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
            )
        content = result.get("content", [])
        if result.get("isError") or len(content) != 1 or content[0].get("type") != "text":
            raise ValueError("Council tool failed")
        payload = json.loads(content[0]["text"])
        if not isinstance(payload, dict):
            raise ValueError("Council tool returned no object")
        return payload


def validate_writ(config: Config, response: dict, command: str) -> WritOfExecution:
    if command not in COMMANDS or response.get("status") != "approved" or not response.get("writ"):
        raise ValueError("Council did not approve the bounded test command")
    writ = WritOfExecution.model_validate_json(base64.b64decode(response["writ"], validate=True))
    if (
        not writ.verify(bytes.fromhex(config.council_key_hex))
        or writ.is_expired()
        or writ.command != command
        or writ.working_directory != config.working_directory
        or writ.target_agent_id != config.agent_id
        or writ.council_id != config.council_id
        or writ.writ_id != response.get("writ_id")
        or writ.packaged_files
        or writ.max_executions != 1
    ):
        raise ValueError("Writ is not a valid authorization for this exact test")
    return writ


def validate_receipt(
    config: Config, data: dict, writ: WritOfExecution, request_id: str, exit_code: int
) -> ExecutionResult:
    result = ExecutionResult.model_validate(data)
    evidence = result.evidence
    if evidence is None or evidence.algorithm != "ed25519" or not evidence.verify():
        raise ValueError("Executor evidence signature is invalid")
    payload = evidence.payload
    fingerprint = hashlib.sha256(bytes.fromhex(evidence.public_key_hex)).hexdigest()
    if (
        evidence.public_key_hex not in config.executor_keys.get(result.executor_id, [])
        or evidence.key_fingerprint != fingerprint
        or payload.executor_key_fingerprint != fingerprint
        or payload.executor_id != result.executor_id
        or payload.writ_id != writ.writ_id
        or result.writ_id != writ.writ_id
        or payload.intent_hash != writ.intent_hash
        or payload.writ_signature != writ.signature
        or payload.target_agent_id != config.agent_id
        or payload.council_id != config.council_id
        or payload.nonce != writ.nonce
        or payload.command.command != writ.command
        or payload.command.working_directory != writ.working_directory
        or payload.request_id != request_id
        or result.exit_code != exit_code
        or result.success != (exit_code == 0)
        or result.verification_stage != "passed"
        or payload.result.exit_code != result.exit_code
        or payload.result.success != result.success
        or payload.result.error != result.error
        or payload.result.verification_stage != result.verification_stage
        or payload.result.stdout_sha256 != hashlib.sha256(result.stdout.encode()).hexdigest()
        or payload.result.stderr_sha256 != hashlib.sha256(result.stderr.encode()).hexdigest()
        or payload.result.stdout_bytes != len(result.stdout.encode())
        or payload.result.stderr_bytes != len(result.stderr.encode())
    ):
        raise ValueError("Executor evidence does not bind the pinned identity and exact result")
    return result


async def run(config: Config, run_id: str, *, client: httpx.AsyncClient) -> dict:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", run_id):
        raise ValueError("Invalid run id")
    report = {
        "run_id": run_id,
        "synthetic": False,
        "scope": "live council, real executor commands, and signed ledger readback",
        "started_at": datetime.now(UTC).isoformat(),
        "council_id": config.council_id,
        "agent_id": config.agent_id,
        "reviews": [],
        "executions": [],
        "passed": False,
        "observer_checked": False,
    }
    council = Council(config, client)
    fields = {
        "agent_id": config.agent_id,
        "auth_token": config.auth_token,
        "council_id": config.council_id,
        "working_directory": config.working_directory,
        "target_resource": f"local-assurance-validation:{run_id}",
        "justification": "Validate asynchronous assurance using bounded commands with no filesystem or network effects.",
        "blast_radius": "Only a no-op process exit in the local OpenShell workspace; no state change to roll back.",
        "context": f"Controlled live assurance validation {run_id}. Normal council review is required.",
    }
    try:
        head = await council.call(
            "council_ledger_page", {"council_id": config.council_id, "limit": 1}
        )
        if (
            head.get("chain_id") != f"ledger-{config.council_id}"
            or type(head.get("head_sequence")) is not int
        ):
            raise ValueError("Unexpected live ledger")
        report["initial_head_sequence"] = head["head_sequence"]
        denied = await asyncio.gather(
            *[
                council.call(
                    "council_propose_intent",
                    {
                        **fields,
                        "command": "true",
                        "description": "Propose a harmless no-op with a deliberately non-absolute path to exercise the policy denial.",
                    },
                )
                for _ in range(3)
            ],
            return_exceptions=True,
        )
        for result in denied:
            if isinstance(result, BaseException):
                raise ValueError("Concurrent denial request failed")
            if (
                not result.get("success")
                or result.get("state") != "denied"
                or not result.get("intent_id")
            ):
                raise ValueError("The expected policy denial was not recorded")
            report["reviews"].append(
                {"kind": "policy_denial", "intent_id": result["intent_id"], "state": "denied"}
            )
        print("PASS: three concurrent council denials; no execution dispatched.", flush=True)

        failed_receipt = None
        for name, command, code in (
            ("benign", "/usr/bin/true", 0),
            ("failure", "/usr/bin/false", 1),
        ):
            authorization = await council.call(
                "council_authorize",
                {
                    **fields,
                    "command": command,
                    "description": f"Run the standard no-op executable {command} in the local OpenShell workspace; it deliberately returns exit code {code} without reading or changing files.",
                },
            )
            writ = validate_writ(config, authorization, command)
            if not authorization.get("decision_id"):
                raise ValueError("Authorization has no ledger decision id")
            report["reviews"].append(
                {
                    "kind": name,
                    "intent_id": authorization["decision_id"],
                    "state": "approved",
                    "members": [
                        {"member_id": vote.get("member_id"), "decision": vote.get("decision")}
                        for vote in authorization.get("council_members", [])
                    ],
                }
            )
            request_id = f"{run_id}:{name}"
            async with (
                asyncio.timeout(config.timeout_seconds),
                client.stream(
                    "POST",
                    config.executor_url.rstrip("/") + "/execute",
                    json={"writ": authorization["writ"], "request_id": request_id},
                    headers={"X-Agent-ID": config.agent_id},
                    timeout=config.timeout_seconds,
                    follow_redirects=False,
                ) as response,
            ):
                response.raise_for_status()
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise ValueError("Executor response exceeds 8 MiB")
            receipt = validate_receipt(config, json.loads(raw), writ, request_id, code)
            report["executions"].append(
                {
                    "kind": name,
                    "command": command,
                    "writ_id": writ.writ_id,
                    "exit_code": receipt.exit_code,
                    "success": receipt.success,
                    "executor_id": receipt.executor_id,
                    "executor_key_fingerprint": receipt.evidence.key_fingerprint,
                    "signature_verified": True,
                }
            )
            if code == 1:
                failed_receipt = receipt
            print(f"PASS: {command} executed with verified signed exit code {code}.", flush=True)

        attached = await council.call(
            "council_propose_intent",
            {
                **fields,
                "command": "/usr/bin/true",
                "description": "Review a harmless no-op following the intentionally failed exit-code probe; its signed executor receipt is attached for audit.",
                "certified_evidence": [failed_receipt.model_dump(mode="json")],
            },
        )
        if not attached.get("success") or not attached.get("intent_id"):
            raise ValueError("Council did not record certified executor evidence")
        report["reviews"].append(
            {
                "kind": "certified_failure_review",
                "intent_id": attached["intent_id"],
                "state": attached.get("state"),
            }
        )
        page = await council.call(
            "council_ledger_page", {"council_id": config.council_id, "limit": 200}
        )
        entries = page.get("entries", [])[::-1]
        if not entries or page.get("chain_id") != head["chain_id"]:
            raise ValueError("Ledger readback failed")
        keys = {
            signature["signer_id"]: config.council_key_hex
            for row in entries
            for signature in row["signatures"]
        }
        verified = verify_batch(
            entries,
            entries[0]["sequence"] - 1,
            entries[0].get("previous_hash"),
            head["chain_id"],
            keys,
        )
        by_intent = {row["payload"].get("intent_id"): row for row in verified}
        sequences = []
        for review in report["reviews"]:
            row = by_intent.get(review["intent_id"])
            if (
                not row
                or row["sequence"] <= head["head_sequence"]
                or row["payload"].get("agent_id") != config.agent_id
                or row["payload"].get("final", {}).get("state") != review["state"]
            ):
                raise ValueError("A live review is missing from the verified ledger")
            if (
                review["kind"] == "policy_denial"
                and row["payload"].get("opa", {}).get("allow") is not False
            ):
                raise ValueError("Denial did not exercise the OPA policy")
            review["sequence"] = row["sequence"]
            review["content_hash"] = row["content_hash"]
            sequences.append(row["sequence"])
        receipt_row = by_intent[attached["intent_id"]]
        summaries = (
            receipt_row["payload"]
            .get("intent", {})
            .get("metadata", {})
            .get("certified_evidence", [])
        )
        if not any(
            summary.get("writ_id") == failed_receipt.writ_id
            and summary.get("success") is False
            and summary.get("exit_code") == 1
            for summary in summaries
        ):
            raise ValueError("Signed ledger lacks the certified failure summary")
        report.update(
            ledger_sequences=sorted(sequences),
            final_head_sequence=page["head_sequence"],
            passed=True,
        )
        print("PASS: all six real council reviews verified in the signed ledger.", flush=True)
    except Exception as exc:
        # Deliberately omit exception text, requests, outputs, tokens, and writ blobs.
        report["error_type"] = type(exc).__name__
        print(f"Live validation stopped: {type(exc).__name__}; no automatic retries.", flush=True)
    report["finished_at"] = datetime.now(UTC).isoformat()
    return report


async def check_observer(
    report: dict, *, client: httpx.AsyncClient, url: str, auth: tuple[str, str]
) -> None:
    """Verify real findings cite the new signed reviews, without exposing UI secrets."""

    async def get(path):
        async with client.stream(
            "GET", url.rstrip("/") + path, auth=auth, timeout=15, follow_redirects=False
        ) as response:
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise ValueError("Observer response exceeds 8 MiB")
            return json.loads(data)

    status = await get("/api/status")
    timeout = analysis_wait_seconds(status)
    async with asyncio.timeout(timeout + 15):
        deadline = time.monotonic() + timeout
        while True:
            if status.get("chain_id") != f"ledger-{report['council_id']}":
                raise ValueError("Observer is bound to a different council ledger")
            state = status.get("worker", {}).get("state")
            if state in {"integrity_error", "worker_error"}:
                raise ValueError("Observer has blocked ingestion or analysis")
            if (
                state == "ok"
                and status["checkpoint"]["sequence"] >= report["final_head_sequence"]
                and status["analysis_sequence"] >= report["final_head_sequence"]
            ):
                break
            if time.monotonic() > deadline:
                raise ValueError("Observer did not reach the new ledger records")
            await asyncio.sleep(0.5)
            status = await get("/api/status")
        findings = (await get("/api/findings"))["findings"]
    expected = {
        "repeated_unchanged_denial": {
            r["sequence"] for r in report["reviews"] if r["kind"] == "policy_denial"
        },
        "explicit_tool_failure": {
            r["sequence"] for r in report["reviews"] if r["kind"] == "certified_failure_review"
        },
    }
    matched = []
    for kind, sequences in expected.items():
        candidates = [
            f
            for f in findings
            if f.get("kind") == kind
            and f.get("agent_id") == report["agent_id"]
            and f.get("synthetic") is False
            and f.get("evidence")
            and all(ref.get("chain_id") == status["chain_id"] for ref in f["evidence"])
            and sequences <= {ref.get("sequence") for ref in f["evidence"]}
        ]
        if not sequences or not candidates:
            raise ValueError("Observer did not produce the expected live finding")
        matched.append({"kind": kind, "id": candidates[0]["id"], "sequences": sorted(sequences)})
    report["observer_checked"] = True
    report["observer"] = {
        "checkpoint_sequence": status["checkpoint"]["sequence"],
        "analysis_sequence": status["analysis_sequence"],
        "findings": matched,
    }
    print(
        "PASS: observer produced both real advisory findings with matching ledger evidence.",
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        type=Path,
        required=True,
        help="Existing Nomos agent/proxy credentials and service URLs.",
    )
    parser.add_argument(
        "--trust-file",
        type=Path,
        required=True,
        help="Existing dashboard trust JSON containing public keys.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New sanitized report; existing files are never overwritten.",
    )
    parser.add_argument(
        "--observer-env-file",
        type=Path,
        help="Optional dashboard credentials for end-to-end finding checks.",
    )
    parser.add_argument("--observer-url", default="http://127.0.0.1:9911")
    parser.add_argument(
        "--run-id", default=datetime.now(UTC).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    )
    args = parser.parse_args()
    os.umask(0o077)
    config = load_config(args.env_file, args.trust_file)
    with args.output.open("x") as output:
        output.write(
            json.dumps({"run_id": args.run_id, "passed": False, "state": "started"}) + "\n"
        )

    async def invoke():
        async with httpx.AsyncClient(trust_env=False) as client:
            report = await run(config, args.run_id, client=client)
            if report["passed"] and args.observer_env_file:
                try:
                    credentials = dotenv_values(args.observer_env_file, interpolate=False)
                    await check_observer(
                        report,
                        client=client,
                        url=args.observer_url,
                        auth=(credentials["DASHBOARD_USER"], credentials["DASHBOARD_PASSWORD"]),
                    )
                except Exception as exc:
                    report.update(passed=False, error_type=type(exc).__name__)
            return report

    report = asyncio.run(invoke())
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
