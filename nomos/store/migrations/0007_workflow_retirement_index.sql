-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0
-- migrate: no-transaction

-- Serves the bounded sweep that deletes retired workflow cache rows. Built
-- concurrently: the cache stays readable and writable while it is built.
CREATE INDEX CONCURRENTLY IF NOT EXISTS council_workflows_retirement_idx
    ON council_workflows (council_id, retire_after) WHERE is_terminal = true;
