-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
-- SPDX-License-Identifier: LicenseRef-NvidiaProprietary
--
-- NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
-- property and proprietary rights in and to this material, related
-- documentation and any modifications thereto. Any use, reproduction,
-- disclosure or distribution of this material and related documentation
-- without an express license agreement from NVIDIA CORPORATION or
-- its affiliates is strictly prohibited.


-- NOMOS-5b: durable, replica-shared council workflow state (minimal-correct).
--
-- Lets ANY council replica answer a status query (check_intent_status) and list
-- pending approvals for an intent that was decided on another replica — the
-- prerequisite for running the council at >1 replica. Without this,
-- `SecurityCouncil._workflows` lives in-process and a status request that
-- load-balances to a different replica returns "Intent not found".
--
-- In the deployed surface a whole review (submit → in-process LLM votes → debate →
-- decide → mint) runs atomically inside ONE request on ONE replica, so the live
-- vote state machine never crosses a replica boundary and needs no shared row
-- lock. This table is therefore a write-through read cache of the workflow's
-- durable state (IntentWorkflow.to_durable_dict()), upserted last-writer-wins by
-- the single replica handling the intent. The full audit trail (votes, debate,
-- decision) still lives in the shared PostgreSQL ledger. Additive: does not touch
-- ledger_chains / ledger_entries / accounts / intents.

CREATE TABLE council_workflows (
    council_id    text        NOT NULL,
    intent_id     text        NOT NULL,
    state         text        NOT NULL,                 -- denormalized WorkflowState (status/pending queries)
    is_terminal   boolean     NOT NULL DEFAULT false,   -- denormalized: drives cross-replica reaping
    workflow_json jsonb       NOT NULL,                 -- IntentWorkflow.to_durable_dict()
    expires_at    timestamptz,                          -- read-time expiry; NULL = never
    version       bigint      NOT NULL DEFAULT 1,        -- bumped per upsert; audit only
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (council_id, intent_id)
);

-- get_pending_approvals(): pending/in-flight lookups by state.
CREATE INDEX council_workflows_state_idx  ON council_workflows (council_id, state);
-- reaper: expired terminal rows.
CREATE INDEX council_workflows_expiry_idx ON council_workflows (expires_at) WHERE is_terminal = true;
