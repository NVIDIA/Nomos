-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0

-- During a rolling upgrade an old process still uses unconditional cache
-- upserts/reaping. Once a row is versioned, those operations must not undo a
-- newer decision or shorten its promised retention. This does not touch audit.
CREATE FUNCTION guard_versioned_workflow_cache() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.logical_version > 0 THEN
        IF TG_OP = 'DELETE' THEN
            IF current_setting('nomos.workflow_cleanup_enabled', true) IS DISTINCT FROM 'on'
               OR OLD.retire_after > clock_timestamp() THEN
                RETURN NULL;
            END IF;
        ELSE
            IF NEW.logical_version <= OLD.logical_version
               OR NEW.retire_after IS NULL
               OR NEW.retire_after < OLD.retire_after
               OR (OLD.is_terminal AND
                   (NOT NEW.is_terminal OR NEW.state <> OLD.state)) THEN
                RETURN NULL;
            END IF;
        END IF;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER council_workflows_cache_fence
    BEFORE UPDATE OR DELETE ON council_workflows
    FOR EACH ROW EXECUTE FUNCTION guard_versioned_workflow_cache();
