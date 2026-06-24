#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
#
# Register an agent account directly into the service-mode PostgreSQL store the
# council reads (mints a token + grants a council). Runs the council image's
# PostgresStore via `docker compose run` — the redis-only agent_accounts CLI is
# gated off in postgres mode, and this needs no agent-signup console image.
# Prints AGENT_TOKEN=<hex>.
#
# Usage: compose-register-agent.sh <agent-id> [council-id]
# Env:   AGENT_ROLE (readonly|developer|operator, default developer)
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./dev-lib.sh
. "$SCRIPT_DIR/dev-lib.sh"

AGENT_ID="${1:?usage: compose-register-agent.sh <agent-id> [council-id]}"
COUNCIL_ID="${2:-council-prod}"
AGENT_ROLE="${AGENT_ROLE:-developer}"

case "$AGENT_ROLE" in
  readonly)  PERMS='{"shell:read", "file:read"}' ;;
  developer) PERMS='{"shell:read", "shell:write", "file:read", "file:write"}' ;;
  operator)  PERMS='{"shell:read", "shell:write", "file:read", "file:write", "infra:deploy"}' ;;
  *) die "AGENT_ROLE must be readonly|developer|operator" ;;
esac

# Reuse the council service image/env (holds COUNCIL_DATABASE_URL +
# COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY); --no-deps since backends are already up.
dc run --rm --no-deps -T \
  -e AGENT_ID="$AGENT_ID" -e COUNCIL_ID="$COUNCIL_ID" \
  --entrypoint python council -c "
import asyncio, hashlib, os, secrets
from nomos.store.postgres_store import PostgresStore
agent = os.environ['AGENT_ID']; council = os.environ['COUNCIL_ID']
token = secrets.token_hex(32)
th = hashlib.sha256(token.encode()).hexdigest()
perms = ${PERMS}
async def main():
    store = PostgresStore(
        os.environ['COUNCIL_DATABASE_URL'],
        # Required (not defaulted): must match the council's key or the account
        # record's HMAC won't validate and the agent silently fails to authenticate.
        account_integrity_key=os.environ['COUNCIL_REDIS_ACCOUNT_INTEGRITY_KEY'],
    )
    await store.connect()
    try:
        await store.set_account(agent, th, agent, True)
        await store.set_token_index(th, agent)
        await store.set_account_permissions(agent, perms)
        await store.grant_account_council(agent, council)
    finally:
        await store.close()
    print('AGENT_TOKEN=' + token, flush=True)
asyncio.run(main())
" 2>/dev/null | sed -n 's/.*\(AGENT_TOKEN=[0-9a-f]\{64\}\).*/\1/p' | tail -n1
