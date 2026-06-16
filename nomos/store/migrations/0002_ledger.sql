-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0

-- Distributed ledger: one canonical hash-linked chain per chain_id, safe for
-- concurrent writers. Sequence assignment happens inside the append
-- transaction (SELECT ... FOR UPDATE on the chain row); the (chain_id,
-- sequence) primary key makes forks structurally impossible.

CREATE TABLE ledger_chains (
    chain_id              text PRIMARY KEY,
    council_id            text NOT NULL,
    created_at            timestamptz NOT NULL DEFAULT now(),
    created_by            text NOT NULL,
    description           text NOT NULL DEFAULT '',
    current_head_sequence bigint,  -- NULL until genesis is written
    current_head_hash     text,
    status                text NOT NULL DEFAULT 'active'
);

CREATE TABLE ledger_entries (
    chain_id            text   NOT NULL REFERENCES ledger_chains(chain_id),
    sequence            bigint NOT NULL CHECK (sequence >= 0),
    entry_id            uuid   NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    entry_type          text   NOT NULL,
    payload_json        jsonb  NOT NULL,
    metadata_json       jsonb  NOT NULL DEFAULT '{}',
    previous_hash       text,   -- NULL only for sequence 0 (genesis)
    entry_hash          text   NOT NULL,
    signatures_json     jsonb  NOT NULL DEFAULT '[]',
    signer_key_id       text,   -- denormalized first signer for audit queries
    writer_instance_id  text   NOT NULL,
    -- The exact timestamp.isoformat() text that the entry hash covers.
    -- Stored verbatim: round-tripping through timestamptz could change the
    -- string and silently break chain validation.
    entry_timestamp_iso text   NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chain_id, sequence),
    UNIQUE (chain_id, entry_hash)
);

CREATE INDEX ledger_entries_created_idx ON ledger_entries (chain_id, created_at);
CREATE INDEX ledger_entries_type_idx ON ledger_entries (chain_id, entry_type);
