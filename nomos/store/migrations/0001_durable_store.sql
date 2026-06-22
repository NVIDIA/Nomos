-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
-- SPDX-License-Identifier: LicenseRef-NvidiaProprietary
--
-- NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
-- property and proprietary rights in and to this material, related
-- documentation and any modifications thereto. Any use, reproduction,
-- disclosure or distribution of this material and related documentation
-- without an express license agreement from NVIDIA CORPORATION or
-- its affiliates is strictly prohibited.


-- Durable store: accounts, token index, permissions, councils, writs,
-- outcomes, intents, trusted executors. Mirrors the Redis key schema in
-- nomos/store/redis_store.py; PostgresStore must preserve its return shapes.

CREATE TABLE accounts (
    agent_id       text PRIMARY KEY,
    token_hash     text NOT NULL,
    display_name   text NOT NULL DEFAULT '',
    enabled        boolean NOT NULL DEFAULT true,
    role           text,
    -- str(time.time()) text for parity with the Redis hash fields
    created_at     text NOT NULL,
    updated_at     text,
    integrity_hmac text NOT NULL DEFAULT '',
    extra          jsonb NOT NULL DEFAULT '{}'
);

CREATE TABLE token_index (
    token_hash text PRIMARY KEY,
    agent_id   text NOT NULL REFERENCES accounts(agent_id) ON DELETE CASCADE
);
CREATE INDEX token_index_agent_idx ON token_index (agent_id);

CREATE TABLE account_permissions (
    agent_id   text NOT NULL REFERENCES accounts(agent_id) ON DELETE CASCADE,
    permission text NOT NULL,
    PRIMARY KEY (agent_id, permission)
);

CREATE TABLE account_councils (
    agent_id   text NOT NULL REFERENCES accounts(agent_id) ON DELETE CASCADE,
    council_id text NOT NULL,
    PRIMARY KEY (agent_id, council_id)
);
CREATE INDEX account_councils_council_idx ON account_councils (council_id);

CREATE TABLE writs (
    writ_id    text PRIMARY KEY,
    writ_json  jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE writ_outcomes (
    writ_id     text PRIMARY KEY,
    exit_code   integer NOT NULL,
    stdout_hash text NOT NULL,
    stderr_hash text NOT NULL,
    executor_id text NOT NULL DEFAULT '',
    executed_at text NOT NULL
);

CREATE TABLE intents (
    intent_id   text PRIMARY KEY,
    agent_id    text,
    intent_json jsonb NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX intents_agent_recent_idx ON intents (agent_id, created_at DESC, intent_id DESC);

CREATE TABLE trusted_executors (
    key_fingerprint text PRIMARY KEY,
    executor_id     text NOT NULL,
    public_key_hex  text NOT NULL,
    enabled         boolean NOT NULL DEFAULT true,
    registered_at   text NOT NULL,
    updated_at      text NOT NULL,
    metadata_json   jsonb NOT NULL DEFAULT '{}'
);
