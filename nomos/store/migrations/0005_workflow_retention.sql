-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0

-- State generation is independent of write completion order. Retirement applies
-- only to the workflow read cache; the signed audit ledger is never deleted.
--
-- Both statements only change the catalog, so the table lock they take is held
-- briefly. The indexes over these columns are built by 0007 and 0008, outside
-- this transaction, so that lock is released before any index is built.
ALTER TABLE council_workflows ADD COLUMN logical_version bigint NOT NULL DEFAULT 0;
ALTER TABLE council_workflows ADD COLUMN retire_after timestamptz;
