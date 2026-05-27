# Security Guidelines

This project brokers privileged actions. Default posture is fail closed.

## Baseline Posture

- Authentication is required by default.
- OPA intent filtering is enabled by default.
- Decision size defaults to unanimous approval. Quorum should only be possible
  when explicitly configured.
- Secrets and private keys must never be committed.
- Runtime env files must be mode `0600`.
- Example executors are examples, not production trust boundaries.

## Secrets

- Use `detect-secrets` before committing.
- Do not whitelist realistic-looking credentials in docs. Use obvious
  placeholders such as `replace-with-agent-auth-token`.
- `.secrets.baseline` entries must be reviewed. Baseline entries should be
  test fixtures, fake hashes, adversarial payloads, or detector false positives.
- Tokens printed by account tooling are one-time values. Store them in a secure
  local env file or secret manager.
- The local agent signup service creates live agent tokens and must remain
  loopback-bound unless it is placed behind real authentication. Its
  unauthenticated HTTP path should stay low-blast-radius by default; use
  `NOMOS_SIGNUP_ALLOWED_ROLES` to keep browser-created accounts below
  operator/admin privilege.
- The signup service `/admin` backend is HTTP Basic auth protected and can
  rotate, disable, delete, and privilege-escalate accounts. Keep
  `NOMOS_SIGNUP_ADMIN_PASSWORD` in `deploy/.env` or a secret manager; exposing
  this backend remotely requires stronger front-door auth and transport
  protection.

## OPA

- The bundled OPA sidecar must load `policies/` at startup.
- If policy files change, recreate the OPA container. OPA does not reload
  mounted policy files automatically in this deployment.
- The configured decision path must return a Data API `result` object with at
  least a boolean `allow`.
- OPA errors and undefined decisions should deny when fail-closed mode is on.

## Execution

- Writ execution must verify signatures, target agent, expiry, nonce, command,
  and packaged file integrity before running anything.
- Executor implementations must conform to `trusted_executor_contract`.
- The in-repo executor is a getting-started implementation. Do not couple core
  council behavior to its internals.
- Prefer absolute executable paths in privileged commands.
- Treat opaque scripts, remote-fetched code, broad recursive mutation, and
  secret/key access as high-risk until proven otherwise.

## Review Expectations

Security-sensitive changes require tests covering both allow and deny paths.
For auth, OPA, writ validation, executor boundaries, or account management,
include regression tests for fail-closed behavior.
