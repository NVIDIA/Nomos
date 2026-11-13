-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0

-- State generation is independent of write completion order. Retirement applies
-- only to the workflow read cache; the signed audit ledger is never deleted.
ALTER TABLE council_workflows ADD COLUMN logical_version bigint NOT NULL DEFAULT 0;
ALTER TABLE council_workflows ADD COLUMN retire_after timestamptz;
CREATE INDEX council_workflows_retirement_idx
    ON council_workflows (council_id, retire_after) WHERE is_terminal = true;
CREATE INDEX council_workflows_decision_expiry_idx
    ON council_workflows (council_id, expires_at)
    WHERE state IN ('approved', 'awaiting_approval');
