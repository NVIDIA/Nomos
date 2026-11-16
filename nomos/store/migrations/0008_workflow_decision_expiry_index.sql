-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0
-- migrate: no-transaction

-- Serves the sweep that expires decisions still open past their deadline.
-- Built concurrently: the cache stays readable and writable while it is built.
CREATE INDEX CONCURRENTLY IF NOT EXISTS council_workflows_decision_expiry_idx
    ON council_workflows (council_id, expires_at)
    WHERE state IN ('approved', 'awaiting_approval');
