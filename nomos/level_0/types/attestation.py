# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Attestation types: the pre-council semantic triage signal.

An attestation vector is the typed, probabilistic answer set returned by the
Jev pre-council stage for a single intent. It is evidence attached to the
intent and shown to the council. The council remains the adjudicator, except
for the opt-in read-only fast path, which mints only when deterministic
exclusions and conservative Jev thresholds all hold.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class QuestionType(StrEnum):
    """Jev question types (the vendor's fixed System One output kinds)."""

    NOUL = "noul"  # yes/no -> probability the statement is true
    SCORE = "score"  # ordered levels -> continuous score
    CHOICE = "choice"  # fixed option set -> chosen option


class Attestation(BaseModel):
    """One answered catalog question for an intent."""

    question_id: str = Field(..., min_length=1, description="Stable id from the question catalog")
    category: str = Field(
        ..., min_length=1, description="Catalog category, e.g. quality/scope/injection"
    )
    qtype: QuestionType = Field(..., description="The Jev question type that was asked")
    value: float | str = Field(
        ..., description="Probability (noul/score) or the chosen option (choice)"
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description=(
            "Confidence in this answer. Only a backend-calibrated number when "
            "detail['confidence_source'] is 'reported'"
        ),
    )
    action: str = Field(
        default="informational",
        description="Catalog policy for this question: informational | flag | deny-candidate",
    )
    tier: str = Field(
        default="attestation",
        description="Catalog tier: triage (fast-path inputs) | risk (chairman) | attestation",
    )
    polarity: str = Field(
        default="adverse",
        description="'adverse' = high value is the concerning reading; 'safe' = the triage gate",
    )
    detail: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Extra per-answer detail. Always carries 'confidence_source': "
            "'reported' when the backend returned a confidence field, "
            "'certainty_proxy' when it was derived as |2p-1| from the answer "
            "itself (not vendor-calibrated), or 'unavailable'. For a score "
            "question it also carries the raw level index, the selected level "
            "label, and the level distribution."
        ),
    )


class AttestationVector(BaseModel):
    """
    The full pre-council attestation for one intent.

    Attached to ``Intent.metadata['attestation']`` and recorded in the ledger.
    The vector never terminates an intent; the chairman and the optional
    read-only fast path derive their decisions from it (``nomos.jev.router``).
    """

    model_config = {"use_enum_values": True}

    model_version: str = Field(
        ...,
        min_length=1,
        description="Concrete model version that answered, e.g. jev-1.13.0 or openjev-4b",
    )
    catalog_version: str = Field(
        ..., min_length=1, description="Version of the question catalog used"
    )
    attestations: list[Attestation] = Field(
        default_factory=list, description="Per-question answers"
    )
    latency_ms: float = Field(default=0.0, ge=0.0, description="Round-trip latency to Jev")
    evaluated_at: datetime | None = Field(default=None, description="When the vector was produced")
    outcome: str = Field(
        default="pass",
        description="Always 'pass': the vector never denies; routing is recorded separately",
    )
    usage: dict[str, Any] = Field(
        default_factory=dict, description="Jev token usage for this call (input/output tokens)"
    )

    def by_id(self, question_id: str) -> Attestation | None:
        """Return the attestation for a question id, or None if absent."""
        for attestation in self.attestations:
            if attestation.question_id == question_id:
                return attestation
        return None

    def to_dict(self) -> dict:
        """Serialize for storage on the intent and in the ledger payload."""
        data = self.model_dump()
        if self.evaluated_at:
            data["evaluated_at"] = self.evaluated_at.isoformat()
        return data
