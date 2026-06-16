-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0

-- NOMOS Tier-1b: idempotency backstop for the consolidated review record.
--
-- The orchestration layer writes exactly one INTENT_REVIEW_RECORDED entry per
-- accepted-and-reviewed intent. intent_id is a random uuid per request, so normal
-- retries get a fresh id and never collide; this partial unique index is a defensive
-- guard against a true duplicate (e.g. a client replaying the same intent_id) so the
-- audit log can never hold two review records for one intent. Additive; scoped to the
-- new entry type, so it does not constrain any historical ledger entries.
CREATE UNIQUE INDEX ledger_entries_review_idem_idx
    ON ledger_entries (chain_id, (payload_json ->> 'intent_id'))
    WHERE entry_type = 'intent_review_recorded';
