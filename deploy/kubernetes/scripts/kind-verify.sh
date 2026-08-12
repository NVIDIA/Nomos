#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Verify a running Nomos Kind dev stack: pod readiness, council MCP, gateway
# token-gate, monitor/signup health, ingress routing, Postgres durable state,
# and a full functional flow (authorize -> writ -> execute -> signed evidence).
# Exits non-zero on any hard failure. Runnable standalone after kind-up.sh.
#
#   deploy/kubernetes/scripts/kind-verify.sh
# Env: AGENT_ID, AGENT_TOKEN (default: NOMOS_* from deploy/.env)
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../scripts/dev-lib.sh
. "$SCRIPT_DIR/../../scripts/dev-lib.sh"

AGENT_ID="${AGENT_ID:-${NOMOS_AGENT_ID:-$(env_get NOMOS_AGENT_ID)}}"
AGENT_ID="${AGENT_ID:-dev-agent}"
AGENT_TOKEN="${AGENT_TOKEN:-$(env_get NOMOS_AUTH_TOKEN)}"
PROXY_TOKEN="$(env_get MONITOR_MCP_PROXY_TOKEN)"

PASS=0; FAIL=0
pass() { ok "$1"; PASS=$((PASS + 1)); }
fail() { printf '  %s\xe2\x9c\x97%s %s\n' "$C_R" "$C_0" "$1" >&2; FAIL=$((FAIL + 1)); }
check() { local label="$1"; shift; if "$@" >/dev/null 2>&1; then pass "$label"; else fail "$label"; fi; }

_deploy_ready() { [[ "$(kcn get deploy "nomos-$1" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)" -ge 1 ]]; }
_sts_ready()    { [[ "$(kcn get statefulset "nomos-$1" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)" -ge 1 ]]; }

log "Verifying Nomos stack (namespace: $NAMESPACE, cluster: kind-$KIND_CLUSTER_NAME)"

# ── pod readiness ───────────────────────────────────────────────────────
log "Workload readiness"
check "statefulset postgres ready" _sts_ready postgres
check "statefulset redis ready"    _sts_ready redis
for d in vault council council-gateway council-grpc monitor executor-openshell openshell opa; do
  check "deploy nomos-$d ready" _deploy_ready "$d"
done
# agent-signup needs the feat/onboarding-console image to run in postgres mode; soft check.
if _deploy_ready agent-signup; then pass "deploy nomos-agent-signup ready"
else warn "deploy nomos-agent-signup NOT ready — build its image from feat/onboarding-console (service-mode console); non-fatal"; fi

# ── internal + ingress HTTP ─────────────────────────────────────────────
log "Service + ingress health"
# council MCP tools (in-cluster, from the council pod itself)
check "council MCP tools registered" kcn exec deploy/nomos-council -c council -- \
  python -c "import urllib.request,sys; d=urllib.request.urlopen('http://localhost:9901/debug/tools/list',timeout=8).read(); sys.exit(0 if b'council_' in d else 1)"

# Ingress-routed endpoints from the host (kind maps :80/:443 -> ingress-nginx).
# Routing is by Host header (vhost), so every check sets Host — unlike compose,
# which hits published ports directly. Helper functions (not `bash -c`) so `check`
# runs them in-process and they can see _code + the script's vars.
_code() { curl -sk -o /dev/null -m 8 -w '%{http_code}' "$@"; }
_ing_gw_healthz() { [[ "$(_code -H 'Host: mcp.nomos.local' http://localhost/healthz)" = 200 ]]; }
_ing_gw_noauth()  { [[ "$(_code -X POST -H 'Host: mcp.nomos.local' http://localhost/mcp)" = 401 ]]; }
_ing_monitor_up() { [[ "$(_code -H 'Host: monitor.nomos.local' http://localhost/)" != 000 ]]; }
check "ingress: gateway /healthz = 200"       _ing_gw_healthz
check "ingress: gateway /mcp no-bearer = 401"  _ing_gw_noauth
check "ingress: monitor reachable"             _ing_monitor_up
if [[ "$(_code -H 'Host: signup.nomos.local' https://localhost/healthz)" = 200 ]]; then
  pass "ingress: agent-signup /healthz = 200"
else warn "ingress: agent-signup /healthz != 200 (see onboarding-console note); non-fatal"; fi

# ── durable state ───────────────────────────────────────────────────────
log "Postgres durable state"
# Query through the council (it holds COUNCIL_DATABASE_URL) rather than guessing pod env.
acct_ok=$(kcn exec deploy/nomos-council -c council -- python -c "
import asyncio,os
from nomos.store.postgres_store import PostgresStore
async def m():
    s=PostgresStore(os.environ['COUNCIL_DATABASE_URL']); await s.connect()
    n=len(await s.list_account_ids()); await s.close(); print(n)
asyncio.run(m())" 2>/dev/null | tail -n1 || echo 0)
if [[ "${acct_ok:-0}" -ge 1 ]]; then pass "Postgres has >=1 registered account ($acct_ok)"; else fail "Postgres has no accounts (registration?)"; fi

# ── functional: authorize -> writ -> execute -> evidence ────────────────
log "Functional flow (authorize -> writ -> execute -> signed evidence)"
if [[ -z "$AGENT_TOKEN" ]]; then
  fail "no AGENT_TOKEN available — cannot run the functional flow"
else
  # Driven from INSIDE the council pod, addressing peers by in-cluster Service DNS:
  # the gateway Service (:8080) and the executor Service (:9907, which targets the
  # container's :9906 — see executorOpenshell.service in values.yaml). Compose's
  # equivalent hits the executor container port 9906 directly — see compose-verify.sh.
  cat > /tmp/nomos_e2e.py <<'PY'
import asyncio, json, os, sys, httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
GW = "http://nomos-council-gateway:8080/mcp"
EXEC = "http://nomos-executor-openshell:9907/v1/writs/execute"
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
                "justification": "kind-verify end-to-end smoke.",
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
  POD="$(kcn get pod -l app.kubernetes.io/component=council -o jsonpath='{.items[0].metadata.name}')"
  kcn cp /tmp/nomos_e2e.py "$POD":/tmp/nomos_e2e.py -c council >/dev/null 2>&1
  if out="$(kcn exec "$POD" -c council -- env AGENT_ID="$AGENT_ID" python /tmp/nomos_e2e.py "$AGENT_TOKEN" "$PROXY_TOKEN" 2>&1)"; then
    echo "$out" | grep -q "AUTHORIZE_OK" && pass "authorize approved + writ minted ($(echo "$out" | grep AUTHORIZE_OK))"
    echo "$out" | grep -q "EXECUTE_OK"   && pass "executed + signed evidence ($(echo "$out" | grep EXECUTE_OK))" || fail "execute/evidence failed: $(echo "$out" | tail -n1)"
  else
    fail "functional flow failed: $(echo "$out" | tail -n2 | tr '\n' ' ')"
  fi
  rm -f /tmp/nomos_e2e.py
fi

# ── verdict ─────────────────────────────────────────────────────────────
echo
if [[ "$FAIL" -eq 0 ]]; then
  log "${C_G}VERIFY PASSED${C_0} — $PASS checks OK"
else
  die "VERIFY FAILED — $FAIL failed, $PASS passed"
fi
