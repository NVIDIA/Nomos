# Deployment Notes

Primary local deployment entrypoint:

```bash
NVIDIA_API_KEY=... ./scripts/deploy_stack.sh
```

The script is intended to work on a clean host with Docker Compose. It creates
or updates `deploy/.env`, forces mode `0600`, starts Redis, OPA, council, gRPC,
the selected executor profile, and optionally the demo UI.

## Defaults

- The internal council MCP service is Docker-network-only. The `monitor`
  proxy owns the host MCP/dashboard port, `9901` by default.
- Published service ports are externally reachable by default. Set
  `NOMOS_PUBLIC_HOST` on remote machines so deploy output and generated signup
  credentials use a reachable IP address. Auto-detection prefers the first
  non-loopback IPv4 address over hostnames.
- Council gRPC publishes on `9904`.
- Agent signup starts by default on `9905`; set
  `WITH_AGENT_SIGNUP=0` to skip it or `AGENT_SIGNUP_PORT=...` to change the
  host port. Browser-created accounts are capped by
  `NOMOS_SIGNUP_ALLOWED_ROLES`, defaulting to `readonly,developer`, and
  `NOMOS_SIGNUP_ALLOWED_COUNCILS`, defaulting to `council-prod`; use account
  CLI tooling or the password-protected `/admin` backend for admin/operator
  credentials, token visibility/rotation, disabling, deletion, council
  membership changes, and live signup council configuration.
  `scripts/deploy_stack.sh` generates `NOMOS_SIGNUP_ADMIN_PASSWORD` in
  `deploy/.env` when omitted.
- Bundled OPA is enabled by default.
- The default trusted executor profile is `executor-openshell`, which starts
  both the OpenShell adapter on host port `9907` and the internal
  `openshell:8080` runtime backend. Use `EXECUTOR_PROFILE=executor-default`
  only when you explicitly want the legacy Docker subprocess executor.
- Demo starts only when `WITH_DEMO=1`.
- Docker deployments mount writable runtime council config from
  `deploy/dynamic-config/`; changes to checked-in `configs/` do not affect an
  already seeded local deployment until the runtime copy is updated or reseeded.
- Prefer dynamic provisioning for council profile changes. Update runtime
  council YAML through the MCP management tools or `scripts/councils.sh` and
  rely on registry hot reload whenever possible. Rebuild images only for code,
  dependency, Dockerfile, or image-baked config changes; restart containers only
  when a component cannot reload the changed state dynamically.

## OPA Reloading

The bundled OPA container loads policy files at startup. Recreate it whenever
policy files change. `deploy_stack.sh` should force-recreate OPA for this
reason.

If the UI returns:

```text
Check that the configured OPA decision path returns a result object.
```

verify OPA loaded policies and that the decision path returns `{"result": ...}`.

## Account Bootstrap

`scripts/deploy_stack.sh` creates a default agent account when missing. If the
account already exists, it does not recover or rotate the token unless
`AGENT_FORCE_ROTATE=1`.

For explicit account management, use:

```bash
python scripts/agent_accounts.py --help
```

## Verification

After deployment:

```bash
NOMOS_GRPC_SMOKE_ADDR="${NOMOS_PUBLIC_HOST:-$(hostname -f)}:9904" uv run pytest tests -q
```

Before committing deployment changes, run the pre-commit hooks and the full
test suite with coverage.
