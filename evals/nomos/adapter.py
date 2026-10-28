# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Fresh council per trial; proposal-only evaluation with no executor capabilities."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

from evals.nomos.workflow import write
from nomos.config.settings import (
    CouncilMemberConfig,
    CouncilSettings,
    OPAConfig,
    SigningConfig,
    StorageConfig,
)
from nomos.level_0.types import CouncilMember
from nomos.llm.base import LLMBackend, LLMResponse
from nomos.llm.council_agent import CouncilMemberAgent, _parse_evaluation
from nomos.nat.functions import CouncilService, ProposeIntentInput


class BoundedBackend(LLMBackend):
    def __init__(self, spec, remaining, delegate=None):
        self.spec, self.remaining, self.delegate = spec, remaining, delegate
        self.calls = 0
        self.failed = False
        self.tokens = []
        self.interactions = []

    def provider_name(self):
        return "offline-evaluation"

    async def complete(self, messages, temperature=0.0, max_tokens=1024, response_format=None):
        if self.calls >= self.remaining:
            self.failed = True
            raise RuntimeError("model call budget exhausted")
        self.calls += 1
        try:
            if self.delegate:
                response = await self.delegate.complete(
                    messages, temperature, max_tokens, response_format
                )
            else:
                key = os.environ["NOMOS_EVAL_API_KEY"]
                async with httpx.AsyncClient(
                    trust_env=False, timeout=self.spec.trial_seconds
                ) as client:
                    result = await client.post(
                        self.spec.endpoint.rstrip("/") + "/chat/completions",
                        headers={"Authorization": f"Bearer {key}"},
                        json={
                            "model": self.spec.model,
                            "messages": [m.model_dump() for m in messages],
                            "temperature": 0,
                            "max_tokens": self.spec.max_output_tokens,
                            "response_format": {"type": "json_object"},
                        },
                    )
                    result.raise_for_status()
                    body = result.json()
                response = LLMResponse(
                    content=body["choices"][0]["message"]["content"],
                    model=self.spec.model,
                    usage=body.get("usage") or {},
                )
            # Nomos intentionally fails closed on malformed model output. In an
            # evaluation, preserve that as an infrastructure/grading failure.
            self.tokens.append(response.usage or None)
            self.interactions.append(
                {"messages": [m.model_dump() for m in messages], "response": response.content}
            )
            parsed = json.loads(response.content)
            if parsed.get("decision") not in {"approve", "deny"}:
                raise ValueError("ungradable model decision")
            _parse_evaluation(response.content, raise_on_parse_error=True)
            return response
        except BaseException:
            self.failed = True
            raise


async def trial(
    request, instruction, spec, trial_id, directory: Path, remaining, *, backend_factory=None
):
    # CouncilService has deployment environment controls (including public-key
    # export paths). Refuse that ambient configuration before creating any state.
    if any(name.startswith("COUNCIL_") and value for name, value in os.environ.items()):
        raise ValueError(
            "run council trials in a clean evaluation environment without COUNCIL_* settings"
        )
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    backend = BoundedBackend(spec, remaining, backend_factory() if backend_factory else None)
    service = CouncilService(
        CouncilSettings(
            council_id=trial_id,
            require_auth=False,
            llm_backends={},
            members=[],
            opa=OPAConfig(enabled=False),
            storage=StorageConfig(mode="redis", dsn=""),
            signing=SigningConfig(provider="local"),
            council_key_path=str(directory / "keys"),
            ledger_path=str(directory / "ledger.jsonl"),
        )
    )
    member = CouncilMemberConfig(
        id="reviewer", backend="evaluation", weight=1, system_prompt_override=instruction
    )
    service._agents.append(CouncilMemberAgent(member_config=member, backend=backend, max_retries=0))
    service.council.register_council_member(
        CouncilMember(
            member_id=member.id, public_key_hex="", display_name="Evaluation reviewer", weight=1
        )
    )
    try:
        # These fixed fields cannot be overridden by captured source content.
        data = dict(request, agent_id=f"evaluation-{trial_id}", council_id=trial_id)
        response = await service.propose_intent(ProposeIntentInput.model_validate(data))
        value = response.model_dump(mode="json")
        write(directory / "response.json", value)
        ledger = service.council.ledger
        from assurance.monitor import verify_batch

        key = service.key_manager.key_pair
        events = verify_batch(
            [ledger.get(i).to_dict() for i in range(ledger.length())],
            -1,
            None,
            ledger.chain_id,
            {key.key_id: key.public_key_hex()},
        )
        write(directory / "verified-ledger.json", events)
        decision = "approve" if response.state == "approved" else "deny"
        result = {
            "status": "error"
            if backend.failed or response.error or not response.council_members
            else "complete",
            "decision": decision,
            "layer": "council",
            "calls": backend.calls,
            "tokens": backend.tokens,
            "intent_id": str(response.intent_id),
            "chain_id": ledger.chain_id,
            "evidence": [
                {"sequence": e["sequence"], "content_hash": e["content_hash"]} for e in events
            ],
            "execution": "not_attempted",
        }
        write(directory / "interactions.json", backend.interactions)
        return result
    finally:
        # Keys remain private trial evidence, and are never reused by another trial.
        for path in directory.rglob("*"):
            if path.is_file():
                path.chmod(0o600)
