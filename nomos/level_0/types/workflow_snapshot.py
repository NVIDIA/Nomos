# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Detached, versioned read-cache updates; these are not authorization receipts."""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class WorkflowSnapshot:
    intent_id: str
    payload_json: str
    logical_version: int
    state: str
    is_terminal: bool
    expires_at: datetime | None
    retire_after: datetime

    @classmethod
    def capture(
        cls,
        *,
        intent_id: str,
        data: dict[str, Any],
        logical_version: int,
        state: str,
        is_terminal: bool,
        expires_at: datetime | None,
        retire_after: datetime,
    ) -> "WorkflowSnapshot":
        if logical_version < 1:
            raise ValueError("workflow snapshot version must be positive")
        if retire_after.tzinfo is None:
            raise ValueError("workflow retirement requires an aware datetime")
        payload = {
            **data,
            "logical_version": logical_version,
            "retire_after": retire_after.isoformat(),
        }
        return cls(
            intent_id,
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            logical_version,
            state,
            is_terminal,
            expires_at,
            retire_after,
        )

    def data(self) -> dict[str, Any]:
        """Decode a fresh copy; no caller can mutate the queued snapshot."""
        return json.loads(self.payload_json)  # type: ignore[no-any-return]
