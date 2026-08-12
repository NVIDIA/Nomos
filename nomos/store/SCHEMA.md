<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Redis Key Schema

All keys are prefixed with a configurable namespace (default: `nomos:`).

## Accounts

| Key | Type | Fields | Description |
|-----|------|--------|-------------|
| `nomos:accounts:{agent_id}` | Hash | `token_hash`, `display_name`, `created_at`, `updated_at`, `enabled`, optional `role` | Agent account info |
| `nomos:accounts:{agent_id}:perms` | Set | Permission strings | Agent permissions |
| `nomos:accounts:{agent_id}:councils` | Set | Council ids, or `*` for all councils | Councils this agent may petition |
| `nomos:token_index:{token_hash}` | String | `agent_id` | Reverse lookup: token hash → agent |
| `nomos:councils:{council_id}:agents` | Set | Agent ids | Reverse index of council members |

## Nonces

| Key | Type | Value | TTL | Description |
|-----|------|-------|-----|-------------|
| `nomos:nonces:{nonce_hex}` | String | `writ_id` | `expires_at - now + 86400` | Atomic single-use claim (SET NX EX) |

## Writs

| Key | Type | Value | Description |
|-----|------|-------|-------------|
| `nomos:writs:{writ_id}` | String (JSON) | Full writ metadata | Writ record |
| `nomos:writs:{writ_id}:outcome` | Hash | `exit_code`, `stdout_hash`, `stderr_hash`, `executed_at`, `executor_id` | Execution outcome |

## Intents

| Key | Type | Value | Description |
|-----|------|-------|-------------|
| `nomos:intents:{intent_id}` | String (JSON) | Intent request + decision + timestamps | Decision record |

## Ledger

| Key | Type | Score | Member | Description |
|-----|------|-------|--------|-------------|
| `nomos:ledger:{council_id}:entries` | Sorted Set | Unix timestamp | Entry hash | Ordered ledger entries |

## Rate Limiting

| Key | Type | Value | TTL | Description |
|-----|------|-------|-----|-------------|
| `nomos:ratelimit:{category}:{identifier}` | String (int) | Count | `window_seconds` | Sliding window counter |

## Permission Strings

```
shell:read       — Non-mutating shell commands (ls, cat, ps, etc.)
shell:write      — Mutating shell commands (mkdir, cp, mv, etc.)
file:read        — Read file contents
file:write       — Create/modify files
file:delete      — Delete files
infra:deploy     — Deploy infrastructure
infra:destroy    — Destroy infrastructure
```
