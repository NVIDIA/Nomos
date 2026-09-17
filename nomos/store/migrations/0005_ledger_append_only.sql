-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
-- SPDX-License-Identifier: LicenseRef-NvidiaProprietary
--
-- NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
-- property and proprietary rights in and to this material, related
-- documentation and any modifications thereto. Any use, reproduction,
-- disclosure or distribution of this material and related documentation
-- without an express license agreement from NVIDIA CORPORATION or
-- its affiliates is strictly prohibited.


-- Append-only ledger: enforce at the database what the application promises.
--
-- Entry hashes are unkeyed, so an actor with write access could rewrite a
-- payload and re-thread the chain, or delete the tail and move the head
-- back, without any hash check noticing. These triggers make that impossible
-- for every role that cannot ALTER the tables (i.e. the runtime role once
-- migrations run under a separate owner — see COUNCIL_DATABASE_MIGRATION_URL):
--
--   ledger_entries : no UPDATE, no DELETE. Ever. INSERT only at the chain's
--                    next sequence (0 while the head is NULL), linked to the
--                    current head hash.
--   ledger_chains  : rows are born without a head; identity columns are
--                    immutable; the head may only advance by exactly one
--                    (or NULL -> 0 at genesis) to a hash that exists at that
--                    sequence. status/description may change.
--
-- TRUNCATE is not covered (test fixtures reset tables with it); it requires
-- TRUNCATE privilege, which the runtime role must not hold.

CREATE OR REPLACE FUNCTION ledger_entries_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'ledger_entries is append-only: % on (%, %) rejected',
        TG_OP, OLD.chain_id, OLD.sequence
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$;

DROP TRIGGER IF EXISTS ledger_entries_append_only ON ledger_entries;
CREATE TRIGGER ledger_entries_append_only
    BEFORE UPDATE OR DELETE ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION ledger_entries_append_only();

-- A new entry must be the chain's next sequence and link to the current head.
-- The writer holds the chain row FOR UPDATE in the same transaction, so the
-- head read here is the one it built the entry against.
CREATE OR REPLACE FUNCTION ledger_entries_next_only() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    head_sequence bigint;
    head_hash text;
BEGIN
    SELECT current_head_sequence, current_head_hash INTO head_sequence, head_hash
      FROM ledger_chains WHERE chain_id = NEW.chain_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ledger_entries insert for unknown chain %', NEW.chain_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.sequence IS DISTINCT FROM COALESCE(head_sequence + 1, 0) THEN
        RAISE EXCEPTION
            'ledger_entries insert must be the next sequence (% expected, % rejected on %)',
            COALESCE(head_sequence + 1, 0), NEW.sequence, NEW.chain_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF head_sequence IS NOT NULL AND NEW.previous_hash IS DISTINCT FROM head_hash THEN
        RAISE EXCEPTION
            'ledger_entries insert % of % does not link to the current head',
            NEW.sequence, NEW.chain_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS ledger_entries_next_only ON ledger_entries;
CREATE TRIGGER ledger_entries_next_only
    BEFORE INSERT ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION ledger_entries_next_only();

-- A chain row is created empty; its head is only ever set through the
-- monotonic UPDATE rule below, so genesis cannot be forged at INSERT time.
CREATE OR REPLACE FUNCTION ledger_chains_born_empty() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.current_head_sequence IS NOT NULL OR NEW.current_head_hash IS NOT NULL THEN
        RAISE EXCEPTION 'ledger_chains rows must be inserted without a head (%)', NEW.chain_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS ledger_chains_born_empty ON ledger_chains;
CREATE TRIGGER ledger_chains_born_empty
    BEFORE INSERT ON ledger_chains
    FOR EACH ROW EXECUTE FUNCTION ledger_chains_born_empty();

CREATE OR REPLACE FUNCTION ledger_chains_head_monotonic() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    expected_hash text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'ledger_chains rows cannot be deleted (%)', OLD.chain_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF NEW.chain_id IS DISTINCT FROM OLD.chain_id
       OR NEW.council_id IS DISTINCT FROM OLD.council_id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.created_by IS DISTINCT FROM OLD.created_by THEN
        RAISE EXCEPTION 'ledger_chains identity columns are immutable (%)', OLD.chain_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    -- Head untouched: status/description edits are fine.
    IF NEW.current_head_sequence IS NOT DISTINCT FROM OLD.current_head_sequence
       AND NEW.current_head_hash IS NOT DISTINCT FROM OLD.current_head_hash THEN
        RETURN NEW;
    END IF;

    IF NEW.current_head_sequence IS NULL OR NEW.current_head_hash IS NULL THEN
        RAISE EXCEPTION 'ledger_chains head cannot be cleared (%)', OLD.chain_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    -- COALESCE keeps this NULL-safe: a NULL head may only become 0.
    IF NEW.current_head_sequence IS DISTINCT FROM COALESCE(OLD.current_head_sequence + 1, 0) THEN
        RAISE EXCEPTION
            'ledger_chains head may only advance by one (% -> % rejected on %)',
            OLD.current_head_sequence, NEW.current_head_sequence, OLD.chain_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    SELECT entry_hash INTO expected_hash
      FROM ledger_entries
     WHERE chain_id = NEW.chain_id AND sequence = NEW.current_head_sequence;
    IF expected_hash IS NULL OR expected_hash <> NEW.current_head_hash THEN
        RAISE EXCEPTION
            'ledger_chains head hash does not match entry % of % (%)',
            NEW.current_head_sequence, NEW.chain_id, NEW.current_head_hash
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS ledger_chains_head_monotonic ON ledger_chains;
CREATE TRIGGER ledger_chains_head_monotonic
    BEFORE UPDATE OR DELETE ON ledger_chains
    FOR EACH ROW EXECUTE FUNCTION ledger_chains_head_monotonic();
