# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
System one-backed council chairman.

Drop-in alternative to the LLM ``ChairmanAgent``. It makes no call of its own:
the council service has already asked system one (member zero) the full catalog, and
this chairman derives the risk level and threshold from that vector in code.
The chairman is one place a system one answer can *relax* scrutiny (quorum instead of
unanimous), and the intent's prose is agent-authored, so quorum is granted only
when every condition holds; a missing or malformed vector is unanimous/high.
"""

import logging

from nomos.config.settings import SystemOneChairmanConfig
from nomos.level_0.types import Intent
from nomos.llm.council_agent import (
    ChairmanAgent,
    ChairmanDecision,
    _hold_to_threshold_floor,
)

from .router import SystemOneRoutingError, chairman_decision

logger = logging.getLogger(__name__)


class SystemOneChairmanAgent(ChairmanAgent):
    """Chairman whose risk assessment comes from the member-zero system one vector."""

    def __init__(
        self, config: SystemOneChairmanConfig, *, model_version: str, allow_quorum: bool = False
    ):
        # ChairmanAgent.__init__ only wires an LLM backend, which this provider does not have.
        self._config = config
        self._model_version = model_version
        self._allow_quorum = allow_quorum

    @property
    def provider_name(self) -> str:
        return f"system_one:{self._model_version}"

    async def decide_threshold(self, intent: Intent) -> ChairmanDecision:
        """Decide the voting threshold for this intent. No usable vector means unanimous."""
        try:
            if intent._attestation is None:
                raise SystemOneRoutingError("no system one attestation for this intent")
            decision = chairman_decision(intent._attestation, self._config)
        except SystemOneRoutingError as e:
            logger.warning("system one chairman failed for %s: %s", intent.intent_id, e)
            return ChairmanDecision(
                threshold="unanimous",
                reasoning=f"system one chairman evaluation failed: {e}. Defaulting to unanimous.",
                risk_level="high",
            )

        decision = _hold_to_threshold_floor(intent, decision, source="system one")
        decision = self._enforce_threshold_policy(decision)
        logger.info(
            "chairman provider=%s intent=%s threshold=%s risk=%s",
            self.provider_name,
            intent.intent_id,
            decision.threshold,
            decision.risk_level,
        )
        return decision
