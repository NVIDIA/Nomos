-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0

-- Install the constraints before scrubbing. CHECK ... NOT VALID tolerates old
-- rows but immediately rejects new violations, closing the race in which an old
-- replica could write raw context after UPDATE had already scanned its row.
ALTER TABLE council_workflows
    ADD CONSTRAINT council_workflows_intent_context_redacted
    CHECK (COALESCE(workflow_json #>> '{intent,context}', '') = '') NOT VALID;

-- Ledger history is immutable, so tolerate old hash-chained rows while enforcing
-- the rule for every new insert. PostgreSQL CHECK ... NOT VALID skips the
-- historical validation scan but still rejects violating rows written after the
-- constraint exists. An old replica therefore fails its authoritative audit
-- append instead of recording new raw context and must not return approval.
ALTER TABLE ledger_entries
    ADD CONSTRAINT ledger_entries_intent_context_redacted
    CHECK (
        (
            entry_type <> 'intent_submitted'
            OR COALESCE(payload_json ->> 'context', '') = ''
        )
        AND
        (
            entry_type <> 'intent_review_recorded'
            OR COALESCE(payload_json #>> '{intent,context}', '') = ''
        )
    ) NOT VALID;

-- Remove raw request context from workflow-cache rows written before durable
-- workflow schema v2. The table is a status/read cache, not the authoritative
-- review record, so context is neither needed for status reads nor safe to keep.
UPDATE council_workflows
SET workflow_json = jsonb_set(
        jsonb_set(
            jsonb_set(
                jsonb_set(workflow_json, '{intent,context}', '""'::jsonb, true),
                '{schema_version}',
                '2'::jsonb,
                true
            ),
            '{redactions}',
            '{"intent_context":"redacted"}'::jsonb,
            true
        ),
        '{context_available}',
        'false'::jsonb,
        true
    ),
    version = version + 1,
    updated_at = now()
WHERE workflow_json ? 'intent';

-- The cache has now been scrubbed, so validate its constraint. This scans the
-- table; production rollout must budget the migration lock/scan accordingly.
-- The ledger constraint intentionally remains NOT VALID because validating it
-- would reject immutable historical rows that predate this privacy contract.
ALTER TABLE council_workflows
    VALIDATE CONSTRAINT council_workflows_intent_context_redacted;
