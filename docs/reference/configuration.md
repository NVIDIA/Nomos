<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Configuration

What you can change without forking Nomos: council members and their models,
the chairman and approval threshold, council and executor config files, and
executor profiles. For the map of swappable parts, see
[What you can swap](../../README.md#what-you-can-swap).

## Council Members

Council members are LLM agents with configurable evaluation priorities. The shipped
configuration uses Nemotron 3.5 Super (`nvidia/nemotron-3.5-super-vl-120b-a12b`) on
[NVIDIA API Catalog](https://build.nvidia.com/) for three voting members with
different security responsibilities:

```yaml
# configs/council.yaml
members:
  - id: command-integrity
    display_name: "Command Integrity Reviewer"
    backend: nim-nemotron-super
    priorities:
      - "Verify command matches the stated intent"
      - "Check for injection or hidden side effects"
      - "Validate parameters are reasonable"

  - id: environment-isolation
    display_name: "Environment Isolation Reviewer"
    backend: nim-nemotron-super
    priorities:
      - "Ensure command stays within scoped resources"
      - "Check for environment variable leaks"

  - id: blast-containment
    display_name: "Blast Containment Reviewer"
    backend: nim-nemotron-super
    priorities:
      - "Assess worst-case impact"
      - "Verify rollback is possible"
```

You can add or remove members to cover your workload’s risks, giving each
reviewer a distinct responsibility such as sensitive-data access or production
impact. Use ordered `priorities` and named `evaluation_criteria` to define those
risks. With the shipped `chairman.allow_quorum: false`, approval is unanimous,
so each member you add is another required approval.

For council-reviewed requests, `chairman.enabled: false` requires
`chairman.allow_quorum: false`: voting is unanimous, there is no chairman call,
and writs use fallback medium risk with a 120-second TTL. With
`chairman.enabled: true` and `chairman.allow_quorum: false`, voting stays
unanimous while retaining the chairman's risk assessment, adaptive risk-based
writ TTL, and audit reasoning useful for backtesting. Keep the chairman's
configured backend when choosing this mode.

To opt into quorum, explicitly set both `chairman.enabled: true` and
`chairman.allow_quorum: true` with a configured chairman backend. The chairman
may then choose quorum subject to existing policy floors. Disabled chairman
with quorum enabled is rejected during configuration validation. The model
defaults are `enabled: false` and `allow_quorum: false`; the shipped YAML profiles
explicitly use `enabled: true` and `allow_quorum: false`.

### One model by default, several if you want them

Each role selects its model independently. `members[].backend`,
`chairman.backend`, and `code_review.backend` name an entry in `llm_backends`,
so any role can use a different model or a different provider. Editing a shared
entry changes every role that references it.

The shipped default uses the same model hosted on
[NVIDIA API Catalog](https://build.nvidia.com/) for all council members, which
keeps deployment to one credential and one endpoint. Distinct models give the
council different review perspectives: each reviewer evaluates with a different
set of strengths.

The commented mixed-model section in [the council configuration](../../configs/council.yaml)
assigns a different open-weight API Catalog model to each voting member:

| Role | Backend | Model |
|---|---|---|
| Command Integrity | `nim-nemotron-super` | `nvidia/nemotron-3.5-super-vl-120b-a12b` |
| Environment Isolation | `nim-deepseek` | `deepseek-ai/deepseek-v4-flash-0731` |
| Blast Containment | `nim-nemotron` | `nvidia/nemotron-3-ultra-550b-a55b` |
| Chairman and code review | `nim-nemotron-super` | `nvidia/nemotron-3.5-super-vl-120b-a12b` |

**One caveat worth stating plainly, because it cuts the other way.** The shipped
`chairman.allow_quorum: false` makes approval unanimous, and a member that cannot
be reached votes deny. Every provider you add is therefore another provider whose
outage denies every request. Model diversity buys independent judgement at the cost of a larger
surface that must all be up at once. If you add providers for robustness, size
`request_timeout_seconds` and `llm_retry_budget_seconds` for the slowest of them,
and consider whether unanimity is still the threshold you want.

The same file has a commented **Alternative direct-vendor backends** block. The
documented example assigns Command Integrity to Claude Opus, Environment
Isolation to Claude Sonnet, Blast Containment to GPT, and both chairman and
code review to Claude Opus.

To activate either example, uncomment the matching `llm_backends` entries and
set each role `backend`. Commented definitions do not run at default startup.
See the [deployment setup guide](../deployment-setup.md) for the provider
credentials each example needs, and
[changing the council model configuration](../deployment-setup.md#changing-the-council-model-configuration)
to apply the change to a running council.

### Other models to consider

These models are not in the shipped default or the commented examples. They are
additional options for review quality and throughput:

| Model | Notes |
|---|---|
| DeepSeek V4 Pro | Strong security-focused model biasing towards more conservative review decisions |
| GLM 5.3 (`z-ai/glm-5.3`) | General-purpose alternative with different review behavior from the shipped default |
| Nemotron 3 Ultra (`nvidia/nemotron-3-ultra-550b-a55b`) | Larger Nemotron alternative when its higher latency is acceptable |

Whichever models you choose, remove active backends that no role references so
their keys are not required at startup. Size `request_timeout_seconds` and
`llm_retry_budget_seconds` for the slowest backend you enable. Editing
`configs/council.yaml` does not change a council that is already running: apply
the change as described in
[changing the council model configuration](../deployment-setup.md#changing-the-council-model-configuration),
then validate with the
[endpoint compatibility checks](../council-mcp-validation.md#public-model-endpoint-compatibility).

> **Upgrading an existing deployment:** Kimi K3 reached end of life on the
> NVIDIA API Catalog on October 15, 2026 and now returns HTTP 410. Nomos does
> not overwrite an already seeded runtime council profile. If your active
> profile still references `moonshotai/kimi-k3` or `nim-kimi`, update the live
> profile to the shipped `nim-nemotron-super` definition and role assignments,
> then run the endpoint compatibility check before restoring authorization
> traffic.

## Configuration Reference

### Council Config (`configs/council.yaml`)

```yaml
council_id: "council-prod"
council_key_path: "/app/keys"

redis:
  host: redis
  port: 6379
  db: 0
  key_prefix: "nomos:"

llm_backends:
  nim-nemotron-super:
    provider: nim
    model: nvidia/nemotron-3.5-super-vl-120b-a12b
    api_key: "${NVIDIA_API_KEY}"
    base_url: "https://integrate.api.nvidia.com/v1"
    max_tokens: 8192

members:
  - id: command-integrity
    display_name: "Command Integrity Reviewer"
    backend: nim-nemotron-super
    priorities: [...]
```

### Executor Config

The executor loads configuration from a YAML file named by `EXECUTOR_CONFIG`. Without it, the executor refuses to start unless `EXECUTOR_UNSAFE_NO_POLICY=1` is set, and that environment-only mode applies only `EXECUTOR_ALLOWED_DIRS` (default `/workspace`) and any regexes in the pipe-separated `EXECUTOR_BLOCKED_PATTERNS`: no tool allowlist, argument checks or denied paths. Use it only for local experiments. YAML config is validated at startup via Pydantic models — invalid values, invalid mode-dependent combinations, or unknown top-level fields produce clear error messages. Omitted fields use model defaults; unsupported nested fields are not uniformly rejected.

**Loading order:** `EXECUTOR_CONFIG` env var → YAML file → env var overrides for select fields.

| Variable | Default | Purpose |
|----------|---------|---------|
| `EXECUTOR_CONFIG` | — | Path to `executor.yaml` (enables YAML mode) |
| `EXECUTOR_PUBLIC_KEY_PATH` | — | Path to council's `council.pub` (32 bytes, raw Ed25519) |
| `EXECUTOR_COUNCIL_URL` | — | Alternative: fetch key from council MCP (fallback) |
| `EXECUTOR_REDIS_URL` | `redis://localhost:6379/0` | Redis for nonce claims |
| `EXECUTOR_REDIS_PREFIX` | `nomos:` | Redis key namespace in environment-only mode; with YAML, set `redis.key_prefix`. Must match the council's `redis.key_prefix` |
| `EXECUTOR_ALLOWED_DIRS` | `/workspace` | Colon-separated allowed working directories |
| `EXECUTOR_HOST` | `127.0.0.1` | Listen address; a non-loopback bind is refused except `0.0.0.0` in container mode |
| `EXECUTOR_CONTAINER_MODE` | — | Set to `1` in containers to permit `0.0.0.0` (also detected from `/.dockerenv`) |
| `EXECUTOR_PORT` | `9906` in the default OpenShell container; `9902` for legacy Docker | Internal listen port |
| `EXECUTOR_OPENSHELL_PORT` | `9907` | Host port published by the default OpenShell Compose profile |
| `EXECUTOR_MAX_TIMEOUT` | `60` | Max command execution time (seconds) in environment-only mode; with YAML, set `executor.max_command_timeout` (`120` in the OpenShell profile, `60` in `executor.yaml`) |
| `EXECUTOR_LOG_LEVEL` | `INFO` | Log verbosity |
| `EXECUTOR_PRIVATE_KEY_PATH` | — | Writable executor-owned Ed25519 key for result evidence |
| `EXECUTOR_PUBLIC_KEY_EXPORT_PATH` | — | Optional raw public-key export path for evidence verification |
| `OPENSHELL_ENDPOINT` | — | OpenShell backend URL for `executor-openshell` |
| `OPENSHELL_REQUEST_TIMEOUT` | `30` | OpenShell backend request timeout |
| `EXECUTOR_UID` / `EXECUTOR_GID` | `10001` | Container process UID/GID (set via compose `user:`) |

Environment variables override the corresponding YAML values for: `EXECUTOR_PUBLIC_KEY_PATH`, `EXECUTOR_COUNCIL_URL`, `EXECUTOR_ALLOWED_DIRS`, `EXECUTOR_REDIS_URL`, `EXECUTOR_REDIS_MODE`, the `EXECUTOR_REDIS_SENTINEL_*` settings, `EXECUTOR_REDIS_CONNECT_TIMEOUT_MS`, `EXECUTOR_REDIS_COMMAND_TIMEOUT_MS`, `EXECUTOR_HOST`, `EXECUTOR_PORT`, `EXECUTOR_LOG_LEVEL`, executor identity paths, and OpenShell backend settings. `EXECUTOR_REDIS_PREFIX`, `EXECUTOR_MAX_TIMEOUT` and `EXECUTOR_BLOCKED_PATTERNS` apply only in environment-only mode.

### Risk-Based TTLs

| Risk Level | Writ TTL | Set By |
|------------|----------|--------|
| `low` | 300 seconds | Chairman pre-auth |
| `medium` | 120 seconds | Chairman pre-auth |
| `high` | 60 seconds | Chairman pre-auth |
| `critical` | 30 seconds | Chairman pre-auth |

---

## Executor Profiles

The executor uses Docker Compose **profiles** to support multiple configurations from a single `docker-compose.yml`. Each profile has its own YAML config, host port, workspace volume, and optionally bind-mounted host directories.

### How Profiles Work

```
docker-compose.yml
├── redis              (always starts, no profile)
├── opa                (always starts, no profile)
├── council            (always starts, no profile)
├── monitor            (always starts, no profile)
├── council-grpc       (always starts, no profile)
├── agent-signup       (always starts, no profile)
├── demo               (always starts, no profile)
├── executor-openshell (profile: executor-openshell, default)
├── openshell          (profile: executor-openshell, isolated runtime)
├── executor-default   (profile: executor-default)
└── executor-dev
                       (profile: executor-dev)
```

A shared YAML anchor (`x-executor-base`) defines the security baseline inherited by all executors:

| Setting | Value |
|---------|-------|
| `cap_drop` | ALL |
| `security_opt` | `no-new-privileges:true` |
| `read_only` | `true` |
| `tmpfs` | `/tmp` (noexec, nosuid, 256MB) |
| `user` | `${EXECUTOR_UID:-10001}:${EXECUTOR_GID:-10001}` |

Each profile then overrides only what it needs: port mapping, config file mount, volumes, and user identity.

### Built-in Profiles

#### `executor-default` — Legacy Restrictive Baseline

```bash
docker compose -f deploy/docker-compose.yml --profile executor-default up -d
```

| Property | Value |
|----------|-------|
| **Host port** | 9902 |
| **Config** | `deploy/executor.yaml` |
| **Timeout** | 60 seconds |
| **Allowed dirs** | `/workspace` |
| **User** | UID 10001 (executor) |
| **Workspace** | Docker volume (`executor-default-workspace`) |
| **Tools** | Safe read/write/dev set (echo, cat, ls, git, python3, curl, etc.) |

#### `executor-dev` — Development Executor

```bash
DEV_REPO_PATH=/path/to/my-project \
EXECUTOR_UID=$(id -u) EXECUTOR_GID=$(id -g) \
    docker compose -f deploy/docker-compose.yml \
    --profile executor-dev up -d
```

| Property | Value |
|----------|-------|
| **Host port** | 9903 (direct — `network_mode: host`) |
| **Config** | `deploy/executor-dev.yaml` |
| **Network** | Host network (required for kind/k8s clusters on `127.0.0.1`) |
| **Timeout** | 120 seconds (longer than the 60s default, for dev builds/tests) |
| **Allowed dirs** | `/workspace`, `/workspace/projects`, `/tmp`, `/workspace/projects/my-project` |
| **User** | Host user's UID/GID (for correct file ownership on bind mounts) |
| **Workspace** | Docker volume + bind-mounted host repo |
| **Extra tools** | `make`, `kubectl` (on top of the standard executor's whitelist) |
| **Extra mounts** | `DEV_REPO_PATH` at `/workspace/projects/my-project`, `${HOME}/.kube` (read-only) |
| **Denied args** | Same as the standard executor: `rm`, `git`, `curl`, `chmod` restrictions. `kubectl` has no per-tool argument restrictions — scope it with the kubeconfig you mount. |

> **Note:** `network_mode: host` means this profile shares the host's network stack. It can reach kind clusters, Redis, and the council on `localhost` directly. This trades some network isolation for k8s accessibility — appropriate for dev, not production.

#### `executor-openshell` — OpenShell Adapter

```bash
docker compose -f deploy/docker-compose.yml --profile executor-openshell up -d
```

| Property | Value |
|----------|-------|
| **Host port** | 9907 |
| **Config** | `deploy/executor-openshell.yaml` |
| **Backend** | Buildable `openshell` runtime service, internal `POST /v1/execute` |
| **Evidence** | Executor-owned Ed25519 key in `executor-openshell-keys` |
| **Runtime network** | `openshell-net`, internal-only and shared only with the trusted adapter |
| **Tools** | Bash, Python 3, and basic local file utilities |

Set `OPENSHELL_ENDPOINT` only when routing the trusted adapter to an external
OpenShell deployment instead of the bundled local runtime.

### UID/GID Passthrough

When bind-mounting host directories, the executor process must run as your host UID so that files it creates are owned by you — not by some container-internal user. The `EXECUTOR_UID` and `EXECUTOR_GID` variables control this:

```bash
# Default OpenShell profile: adapter runs as UID 10001 and runtime as UID 10002
docker compose -f deploy/docker-compose.yml --profile executor-openshell up -d

# Dev profile: runs as YOUR user so bind-mounted files are owned correctly
EXECUTOR_UID=$(id -u) EXECUTOR_GID=$(id -g) \
    docker compose -f deploy/docker-compose.yml --profile executor-dev up -d
```

The default profile defaults to UID 10001 (no host mounts needed). Dev profiles default to UID 1000 but should always be started with explicit `EXECUTOR_UID`/`EXECUTOR_GID`.

---

## Executor YAML Configuration

Each executor profile is driven by a YAML file mounted into the container at `/app/config/executor.yaml`. The file is validated at startup by Pydantic models (`executors/docker/config_schema.py`) — invalid regex, bad ports, relative paths, or unknown top-level keys all produce clear error messages. Unknown keys inside a section are ignored, so check spelling there.

### Default OpenShell Profile Shape

Abridged from `deploy/executor-openshell.yaml`; lists marked `...` are shortened.

```yaml
# ── Executor server ────────────────────────────────────
executor:
  listen_host: "0.0.0.0"           # bind address
  listen_port: 9906                 # internal OpenShell adapter port; host defaults to 9907
  log_level: "INFO"                 # DEBUG, INFO, WARNING, ERROR, CRITICAL
  max_command_timeout: 120          # hard limit per command (seconds)
  identity_private_key_path: "/app/executor-keys/executor.key"
  identity_public_key_path: "/app/executor-keys/executor.pub"
  passthrough_env: []               # host env vars passed to commands

# ── Council connection ─────────────────────────────────
council:
  public_key_path: "/app/public-keys/council.pub"  # Ed25519 public key
  url: "http://council:9901"                # fallback: fetch key via HTTP

# ── Redis ──────────────────────────────────────────────
redis:
  url: "redis://redis:6379/0"
  key_prefix: "nomos:"               # must match council's prefix

# ── OpenShell backend ──────────────────────────────────
openshell:
  endpoint: "http://openshell:8080"
  request_timeout: 30

# ── Filesystem access ──────────────────────────────────
filesystem:
  allowed_working_dirs:            # ONLY these dirs can be working_directory
    - /workspace
  denied_paths:                    # substring match — always rejected
    - /etc/shadow
    - .ssh
    - .aws
    # ...

# ── Tool whitelist ─────────────────────────────────────
tools:
  allowed:                         # empty list = allow all (not recommended)
    - echo
    - cat
    - bash
    - python3
    # ...
  denied_args:                     # per-tool regex restrictions
    bash:
      - '-i'
      - '--init-file'
      - '--rcfile'

# ── Security ───────────────────────────────────────────
security:
  blocked_patterns:                # extra regex patterns to block
    - '\bsudo\b'
    - '/dev/tcp/'
    # ...
```

The schema also accepts `council.key_fetch_retries` and `council.key_fetch_delay`, but the executor does not read them yet: key fetches always retry 10 times, 3 seconds apart.

### Security Layers (Defense in Depth)

The executor enforces **four independent policy layers**, each configurable per-profile:

| Layer | Config Key | How It Works | Recommended defaults (shipped in `executor.yaml` / `executor-dev.yaml`) |
|-------|-----------|--------------|---------------------|
| **Blocked patterns** | `security.blocked_patterns` | Regex match against full command string | Fork bombs, sudo, su, pipe-to-shell, raw device access, mkfs, netcat, http.server, rm --no-preserve-root, chmod 777 |
| **Tool whitelist** | `tools.allowed` | Only listed binaries can execute (basename match) | Safe read/write/dev set |
| **Denied tool args** | `tools.denied_args` | Per-tool regex against full command string | rm -rf /, git push --force, git reset --hard, curl PUT/DELETE, chmod 777 |
| **Denied paths** | `filesystem.denied_paths` | Substring match against full command string | /etc/shadow, /etc/passwd, /root, /proc, /sys, .ssh, .gnupg, .aws (plus .kube/config in the production profile) |

These four **policy layers carry no built-in rules** — they are entirely YAML-driven, and each collection defaults to empty (`executors/docker/config.py`; general executor settings such as ports and timeouts still have code-level defaults, but the security policy does not). The protections above are the recommended set shipped in the `deploy/executor.yaml` and `executor-dev.yaml` profiles (the OpenShell profile ships a lighter set); and because nothing is merged in, editing a profile YAML can **weaken or remove** them — for example, an empty `tools.allowed` disables the whitelist and allows every tool. Keep the shipped defaults unless you have a specific reason to change them, and review policy edits carefully.

### Blocked Patterns (Recommended Set)

The `executor-default` and `executor-dev` profiles ship these patterns in their YAML (the OpenShell profile ships a subset):

```yaml
security:
  blocked_patterns:
    # credential / secret exfiltration
    - 'env\s.*(?:SECRET|TOKEN|KEY|PASS)'
    - 'printenv\s.*(?:SECRET|TOKEN|KEY|PASS)'
    - 'base64.*(?:\.pem|\.key|\.crt)'
    # network exfiltration
    - 'curl\s+.*-d\s+@'
    - 'curl\s+.*--upload-file'
    - 'wget\s+--post-file'
    # reverse shells
    - '/dev/tcp/'
    - 'bash\s+-i\s+>'
    - 'exec\s+\d+<>/dev/tcp'
    # history / credential file reads
    - 'cat.*\.bash_history'
    - 'cat.*\.env'
    # package supply-chain
    - 'pip\s+install\s+--index-url'
    - 'npm\s+config\s+set\s+registry'
```

---

## Creating a Custom Executor Profile

This section configures another profile of the bundled executor. To replace the
executor with your own service, implement the
[trusted executor API](mcp-api.md#trusted-executor-api) instead.

To create an executor profile for your own project, you need three things:

1. **A YAML config file** — defines the security policy
2. **A service entry in docker-compose.yml** — wires up ports, volumes, and identity
3. **Optionally: Dockerfile changes** — if you need additional system packages

### Step 1: Create Your Config YAML

Copy the default config and customize it:

```bash
cp deploy/executor.yaml deploy/executor-my-project.yaml
```

Edit the key sections:

```yaml
executor:
  max_command_timeout: 180          # adjust for your workload

filesystem:
  allowed_working_dirs:
    - /workspace
    - /home/youruser/Development/my-project   # bind-mounted host repo
  denied_paths:
    - /etc/shadow
    - /etc/passwd
    - .ssh
    - .gnupg
    - .aws

tools:
  allowed:
    # start with the minimum set you need
    - echo
    - cat
    - ls
    - grep
    - find
    - mkdir
    - cp
    - rm
    - touch
    - python3
    - pip
    - git
    # no shell interpreters (sh, bash): the enforcer would check `sh`, not the
    # command it runs, so they bypass this allowlist
    # add project-specific tools:
    - npm
    - node
    - kubectl
  denied_args:
    rm:
      - '-rf\s+/\s*$'
      - '--no-preserve-root'
    git:
      - 'push\s+--force'
      - 'reset\s+--hard'

security:
  blocked_patterns:
    - 'env\s.*(?:SECRET|TOKEN|KEY|PASS)'
    - '/dev/tcp/'
    - 'bash\s+-i\s+>'
    # add project-specific patterns as needed
```

### Step 2: Add a Compose Service

Add a new service to `deploy/docker-compose.yml` that inherits from the shared base:

```yaml
  # ── Executor: My Project (dev) ────────────────────────────
  executor-my-project:
    <<: *executor-base
    profiles: ["executor-my-project"]
    user: "${EXECUTOR_UID:-1000}:${EXECUTOR_GID:-1000}"   # host user for bind mounts
    ports:
      - "9909:9902"                                        # unique host port; 9905 is agent-signup
    environment:
      - EXECUTOR_PUBLIC_KEY_PATH=/app/public-keys/council.pub
      - EXECUTOR_REDIS_URL=redis://:${REDIS_PASSWORD:-nomos-dev}@redis:6379/0
      - EXECUTOR_HOST=0.0.0.0
      - EXECUTOR_PORT=9902
      - EXECUTOR_CONTAINER_MODE=1
      - EXECUTOR_CONFIG=/app/config/executor.yaml
    volumes:
      - council-public-keys:/app/public-keys:ro
      - ./executor-my-project.yaml:/app/config/executor.yaml:ro
      - executor-my-project-keys:/app/executor-keys            # writable: the executor's signing key
      - executor-my-project-workspace:/workspace
      # Bind-mount your host repo (same path inside and outside)
      - /home/youruser/Development/my-project:/home/youruser/Development/my-project
```

Add the workspace volume:

```yaml
volumes:
  # ... existing volumes ...
  executor-my-project-keys:
    driver: local
  executor-my-project-workspace:
    driver: local
```

### Step 3: Mount Credentials (Optional)

If your project needs kubectl, cloud CLI, or other credential-based tools:

```yaml
    environment:
      # ... existing env vars ...
      - HOME=/home/executor                         # the AWS CLI finds ~/.aws/credentials from HOME
      - KUBECONFIG=/home/executor/.kube/config
      - AWS_CONFIG_FILE=/home/executor/.aws/config
    volumes:
      # ... existing volumes ...
      - ${HOME}/.kube:/home/executor/.kube:ro          # always read-only!
      - ${HOME}/.aws:/home/executor/.aws:ro
```

The executor does not forward its environment to the commands it runs. List the
variables in your executor YAML so they reach `kubectl` and the cloud CLI:

```yaml
executor:
  passthrough_env:
    - HOME
    - KUBECONFIG
    - AWS_CONFIG_FILE
```

`passthrough_env` refuses names that look like secrets (containing `SECRET`,
`TOKEN`, `PASSWORD`, `PASSWD` or `CREDENTIAL`, or ending in `_KEY`), so point
tools at credential files through `HOME` rather than variables such as
`AWS_SHARED_CREDENTIALS_FILE`.

**Important:** Always mount credentials as `:ro` (read-only). Remove the corresponding entry from `denied_paths` in your YAML config if you intentionally mount it (e.g., remove `.kube/config` if you mount `~/.kube`).

### Step 4: Add System Packages (Optional)

If your project needs packages not in the base image, you have two options:

**Option A: Modify `Dockerfile.executor`** (shared by all profiles)

```dockerfile
# Add to the apt-get install block:
RUN apt-get update && apt-get install -y --no-install-recommends \
    your-package \
    && rm -rf /var/lib/apt/lists/*
```

**Option B: Use a separate Dockerfile** (profile-specific)

```yaml
  executor-my-project:
    build:
      context: ..
      dockerfile: deploy/Dockerfile.executor.my-project   # custom Dockerfile
    # ... rest of config ...
```

### Step 5: Start and Test

```bash
# Start your profile
EXECUTOR_UID=$(id -u) EXECUTOR_GID=$(id -g) \
    docker compose -f deploy/docker-compose.yml \
    --profile executor-my-project up -d --build

# Verify health
curl -s http://127.0.0.1:9909/v1/health | python3 -m json.tool

# Run E2E tests against your profile
uv run --env-file deploy/.env python scripts/test_live_e2e.py 9909 deploy-executor-my-project-1
```

### Path Matching Strategy

When agents formulate commands, the `working_directory` in the writ must match an entry in `allowed_working_dirs`. For bind-mounted host directories, **use the same path inside and outside the container** so agents don't need to translate paths:

```yaml
# In your executor YAML:
filesystem:
  allowed_working_dirs:
    - /home/youruser/Development/my-project

# In docker-compose.yml volumes:
volumes:
  - /home/youruser/Development/my-project:/home/youruser/Development/my-project
```

This way, when the agent says `working_directory: /home/youruser/Development/my-project`, the executor sees the same path and the files are the same.

### Checklist for New Profiles

- [ ] YAML config file created in `deploy/`
- [ ] `allowed_working_dirs` includes all directories the agent needs
- [ ] `tools.allowed` is minimal — only what the project requires
- [ ] `denied_paths` excludes sensitive files (credentials, system config)
- [ ] `security.blocked_patterns` is populated (not empty)
- [ ] Compose service added with unique host port
- [ ] `user:` set to `${EXECUTOR_UID}:${EXECUTOR_GID}` if using bind mounts
- [ ] Credential mounts are `:ro`
- [ ] Workspace volume declared in `volumes:` section
- [ ] Health check verified: `curl http://localhost:<port>/v1/health`

---
