-- Restore the established contract that Intent context used during review is
-- retained in workflow and ledger records. The draft 0005 migration may have
-- been applied by a development deployment before it was removed from this
-- branch, so a forward migration is required to remove its database guards.
--
-- Context values scrubbed by 0005 cannot be reconstructed. This migration only
-- permits subsequent records to retain the sanitized evidence supplied by the
-- caller.

ALTER TABLE IF EXISTS council_workflows
    DROP CONSTRAINT IF EXISTS council_workflows_intent_context_redacted;

ALTER TABLE IF EXISTS ledger_entries
    DROP CONSTRAINT IF EXISTS ledger_entries_intent_context_redacted;
