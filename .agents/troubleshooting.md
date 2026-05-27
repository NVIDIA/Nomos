# Troubleshooting

## OPA Returns Undefined Decision

Symptom:

```text
Check that the configured OPA decision path returns a result object.
```

Likely cause: OPA is running without loaded policies or the configured decision
path points at the wrong package/rule.

Checks:

```bash
docker compose -f deploy/docker-compose.yml logs --tail=80 opa
docker compose -f deploy/docker-compose.yml exec -T council \
  curl -sS http://opa:8181/v1/policies
```

Expected decision path:

```text
/v1/data/nomos/intent/decision
```

Expected response shape:

```json
{"result":{"allow":true,"reasons":[],"guidance":[],"metadata":{"policy":"nomos.intent","version":"1"}}}
```

Fix:

```bash
docker compose -f deploy/docker-compose.yml up -d --force-recreate opa
```

## Demo Has Stale UI Assets

The demo sets no-store headers for `/` and `/static/*`. If stale assets still
appear, rebuild/restart the demo:

```bash
docker compose -f deploy/docker-compose.yml up -d --build demo
```

## Existing Agent Token Is Unknown

If account bootstrap reports `NOMOS_ACCOUNT_STATUS=exists`, the token was not
recovered. Rotate it intentionally:

```bash
AGENT_FORCE_ROTATE=1 ./scripts/deploy_stack.sh
```

## Coverage Hook Fails

Run the same command locally:

```bash
uv run pytest tests -q --cov --cov-report=term-missing --cov-fail-under=77
```

Add focused unit tests for uncovered behavior. Do not lower the coverage floor
unless the reduction is intentional and documented.
