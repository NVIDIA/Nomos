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
# Verify a running Nomos docker-compose dev stack: service health, council MCP,
# gateway token-gate, monitor/signup health, Postgres durable state, and a full
# functional flow (authorize -> writ -> execute -> signed evidence via Vault transit).
# Exits non-zero on any hard failure. Runnable standalone after compose-up.sh.
#
#   deploy/scripts/compose-verify.sh
# Env: AGENT_ID, AGENT_TOKEN (default: NOMOS_* from deploy/.env)
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./dev-lib.sh
. "$SCRIPT_DIR/dev-lib.sh"

AGENT_ID="${AGENT_ID:-${NOMOS_AGENT_ID:-$(env_get NOMOS_AGENT_ID)}}"
AGENT_ID="${AGENT_ID:-dev-agent}"
AGENT_TOKEN="${AGENT_TOKEN:-$(env_get NOMOS_AUTH_TOKEN)}"
PROXY_TOKEN="$(env_get MONITOR_MCP_PROXY_TOKEN)"
GATEWAY_PORT="$(env_get COUNCIL_GATEWAY_PORT)"; GATEWAY_PORT="${GATEWAY_PORT:-9900}"
MONITOR_PORT="$(env_get MONITOR_PORT)"; MONITOR_PORT="${MONITOR_PORT:-9901}"
SIGNUP_PORT="$(env_get AGENT_SIGNUP_PORT)"; SIGNUP_PORT="${SIGNUP_PORT:-9905}"

PASS=0; FAIL=0
pass() { ok "$1"; PASS=$((PASS + 1)); }
fail() { printf '  %s\xe2\x9c\x97%s %s\n' "$C_R" "$C_0" "$1" >&2; FAIL=$((FAIL + 1)); }
check() { local label="$1"; shift; if "$@" >/dev/null 2>&1; then pass "$label"; else fail "$label"; fi; }

_svc_healthy() {
  local cid state
  cid="$(dc ps -q "$1" 2>/dev/null | head -n1)"; [ -n "$cid" ] || return 1
  state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || echo none)"
  [ "$state" = healthy ] || [ "$state" = running ]
}
_code() { curl -sk -o /dev/null -m 8 -w '%{http_code}' "$@"; }
# Gateway/monitor checks as functions so `check` runs them in-process (a `bash -c`
# subshell would not see _code or the script globals).
_gw_healthz()  { [ "$(_code http://localhost:${GATEWAY_PORT}/healthz)" = 200 ]; }
_gw_noauth()   { [ "$(_code -X POST http://localhost:${GATEWAY_PORT}/mcp)" = 401 ]; }
_gw_auth()     { local c; c="$(_code -X POST -H "Authorization: Bearer ${PROXY_TOKEN}" http://localhost:${GATEWAY_PORT}/mcp)"; [ "$c" != 401 ] && [ "$c" != 000 ]; }
_monitor_up()  { [ "$(_code http://localhost:${MONITOR_PORT}/)" != 000 ]; }

log "Verifying Nomos compose stack (project: $COMPOSE_PROJECT)"

# ── service health ───────────────────────────────────────────────────────
log "Service health"
check "postgres healthy" _svc_healthy postgres
check "vault healthy"    _svc_healthy vault
for s in redis redis-1 redis-2 redis-sentinel-1 redis-sentinel-2 redis-sentinel-3 \
         council council-gateway council-grpc monitor executor-openshell openshell opa; do
  check "service $s healthy" _svc_healthy "$s"
done
if _svc_healthy agent-signup; then pass "service agent-signup healthy"
else warn "service agent-signup NOT healthy — build its image from feat/onboarding-console (service-mode console); non-fatal"; fi

# ── council MCP + gateway token-gate ─────────────────────────────────────
log "Council MCP + gateway"
check "council MCP tools registered" dc exec -T council \
  python -c "import urllib.request,sys; d=urllib.request.urlopen('http://localhost:9901/debug/tools/list',timeout=8).read(); sys.exit(0 if b'council_' in d else 1)"
check "gateway /healthz = 200"          _gw_healthz
check "gateway /mcp no-bearer = 401"    _gw_noauth
check "gateway /mcp with-bearer != 401" _gw_auth
check "monitor reachable"               _monitor_up
if [ "$(_code https://localhost:${SIGNUP_PORT}/healthz 2>/dev/null)" = 200 ]; then
  pass "agent-signup /healthz = 200"
else warn "agent-signup /healthz != 200 (see onboarding-console note); non-fatal"; fi

# ── durable state ─────────────────────────────────────────────────────────
log "Postgres durable state"
acct_ok=$(dc exec -T council python -c "
import asyncio,os
from nomos.store.postgres_store import PostgresStore
async def m():
    s=PostgresStore(os.environ['COUNCIL_DATABASE_URL']); await s.connect()
    n=len(await s.list_account_ids()); await s.close(); print(n)
asyncio.run(m())" 2>/dev/null | tail -n1 || echo 0)
if [ "${acct_ok:-0}" -ge 1 ]; then pass "Postgres has >=1 registered account ($acct_ok)"; else fail "Postgres has no accounts (registration?)"; fi

# ── functional: authorize -> writ -> execute -> evidence ──────────────────
log "Functional flow (authorize -> writ -> execute -> signed evidence)"
if [ -z "$AGENT_TOKEN" ]; then
  fail "no AGENT_TOKEN available — cannot run the functional flow"
else
  cat > /tmp/nomos_compose_e2e.py <<'PY'
import asyncio, json, os, sys, httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
GW = "http://council-gateway:8080/mcp"
EXEC = "http://executor-openshell:9906/v1/writs/execute"
TOKEN, PROXY = sys.argv[1], sys.argv[2]
async def main():
    async with streamablehttp_client(GW, headers={"Authorization": "Bearer " + PROXY}) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            out = json.loads((await s.call_tool("council_authorize", arguments={
                "agent_id": os.environ.get("AGENT_ID", "dev-agent"), "auth_token": TOKEN,
                "council_id": "council-prod", "command": "/bin/echo nomos-verify",
                "working_directory": "/workspace",
                "description": "Print a constant string to verify the execution path.",
                "target_resource": "shell://dev/echo", "target_environment": "development",
                "justification": "compose-verify end-to-end smoke.",
                "blast_radius": "None; prints a constant, no state change."})).content[0].text)
            if out.get("status") != "approved" or not out.get("writ"):
                print("AUTHORIZE_FAIL status=%s err=%s" % (out.get("status"), (out.get("error") or "")[:200])); sys.exit(1)
            print("AUTHORIZE_OK members=%d risk=%s" % (len(out.get("council_members", [])), (out.get("chairman") or {}).get("risk_level")))
            async with httpx.AsyncClient(timeout=40) as c:
                ej = (await c.post(EXEC, json={"writ": out["writ"]}, headers={"X-Agent-ID": os.environ.get("AGENT_ID", "dev-agent")})).json()
            if not ej.get("success") or "evidence" not in ej:
                print("EXECUTE_FAIL success=%s keys=%s" % (ej.get("success"), sorted(ej.keys()))); sys.exit(1)
            print("EXECUTE_OK exit=%s stdout=%r evidence=yes" % (ej.get("exit_code"), (ej.get("stdout") or "").strip()))
asyncio.run(main())
PY
  POD="$(dc ps -q council | head -n1)"
  docker cp /tmp/nomos_compose_e2e.py "$POD":/tmp/nomos_compose_e2e.py >/dev/null 2>&1
  if out="$(dc exec -T -e AGENT_ID="$AGENT_ID" council python /tmp/nomos_compose_e2e.py "$AGENT_TOKEN" "$PROXY_TOKEN" 2>&1)"; then
    echo "$out" | grep -q "AUTHORIZE_OK" && pass "authorize approved + writ minted ($(echo "$out" | grep AUTHORIZE_OK))"
    echo "$out" | grep -q "EXECUTE_OK"   && pass "executed + signed evidence ($(echo "$out" | grep EXECUTE_OK))" || fail "execute/evidence failed: $(echo "$out" | tail -n1)"
  else
    fail "functional flow failed: $(echo "$out" | tail -n2 | tr '\n' ' ')"
  fi
  rm -f /tmp/nomos_compose_e2e.py
fi

# ── verdict ───────────────────────────────────────────────────────────────
echo
if [ "$FAIL" -eq 0 ]; then
  log "${C_G}VERIFY PASSED${C_0} — $PASS checks OK"
else
  die "VERIFY FAILED — $FAIL failed, $PASS passed"
fi
