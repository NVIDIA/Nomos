<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Development

Repository layout, local tests, and the in-process kernel example. External
contributions are not currently accepted; see [MAINTAINERS.md](../MAINTAINERS.md).

## In-Process Kernel Example

This drives the kernel directly with a manually registered agent and a manual
approval. It is a testing path, not how a deployed agent integrates; for that,
see [Integrate your own agent](integrate-your-agent.md).

```python
from nomos.level_0.types import Intent, CouncilMember, AgentRole
from nomos.level_4.council import SecurityCouncil, CouncilConfig
from nomos.level_3.executor import NoOpHandler

config = CouncilConfig(
    council_id="my-council",
    require_justification=True,
    default_token_ttl_seconds=300,
)
council = SecurityCouncil(config=config)
council.register_agent("my-agent", AgentRole.APPLICATION)
council.register_council_member(
    CouncilMember(member_id="alice", public_key_hex="abc", display_name="Alice", weight=1)
)
council.register_action_handler(NoOpHandler())

intent = Intent.create(
    agent_id="my-agent",
    action_type="system.noop",
    description="Test action",
    target_resource="/test",
    justification="Testing the system",
)

result = council.submit_intent(intent)
if result.requires_approval:
    council.approve_intent(intent.intent_id, "alice")
exec_result = council.execute_intent(intent.intent_id)
print(f"Success: {exec_result.success}")
```

## Testing

### Local development checks

Install the development dependencies and commit hooks once per checkout:

```bash
uv run --extra dev pre-commit install
```

Run the normal hooks manually when needed:

```bash
uv run --extra dev pre-commit run --all-files
```

The slower coverage gate runs in CI. Run it locally when needed with:

```bash
uv run --extra dev pre-commit run unit-tests-coverage --hook-stage manual
```

### Test Suite Overview

The core suite currently collects 1,400+ tests:

```bash
# Lightweight development run; optional NAT MCP tests skip
uv sync --extra dev
uv run pytest tests/ -v

# Full optional-backend run, matching CI dependencies
uv sync --extra all
uv run pytest tests/ -v

# Standard local regression wrapper and report
uv run ./scripts/run_test_suite.py --profile integration

# Deployed MCP/gRPC checks after Kind or Compose is running
uv run ./scripts/run_test_suite.py \
  --suite grpc-live \
  --suite mcp-live \
  --require-deployed

# By layer
uv run pytest tests/level_0/   # Clock, Crypto, Types
uv run pytest tests/level_1/   # Ledger, Policy
uv run pytest tests/level_2/   # Capability, Quorum
uv run pytest tests/level_3/   # Executor
uv run pytest tests/level_4/   # Council integration

# Writ system
uv run pytest tests/test_writ_factory.py
uv run pytest tests/test_enforcer.py
uv run pytest tests/test_e2e_writ_lifecycle.py
uv run pytest tests/test_key_rotation.py

# Auth system
uv run pytest tests/test_authenticator.py
uv run pytest tests/test_chairman_preauth.py
uv run pytest tests/test_permissions.py

# Infrastructure
uv run pytest tests/test_redis_store.py
```

### Live E2E Tests (Requires Running Stack)

Run these only against a disposable workspace: the script's cleanup deletes
every `*.txt` file in the executor's `/workspace`, not only the files it created.

```bash
# Compose-local E2E against the default OpenShell executor (port 9907)
uv run --env-file deploy/.env python scripts/test_live_e2e.py

# Compose-local E2E against a specific executor profile
uv run --env-file deploy/.env python scripts/test_live_e2e.py 9903 deploy-executor-dev-1
```

The current script tests **3 live scenarios** against Compose containers:

- **Authorized execution** — council approves, Writ is minted, executor creates a file, and the file is verified.
- **Full-path compliance** — bare `echo` is denied while `/bin/echo` is approved for the same low-risk intent.
- **Destructive command review** — council evaluates `rm -rf` and prints the decision and reasoning.

For the full post-deploy MCP validation path, including Kind-safe checks, see
[Council MCP Validation](council-mcp-validation.md).

---

## Project Structure

```
Nomos/
├── nomos/
│   ├── level_0/              # Clock, Crypto, Types (WritOfExecution, Intent)
│   ├── level_1/              # LedgerChain, PolicyEngine
│   ├── level_2/              # CapabilityService, QuorumManager
│   ├── level_3/              # SandboxExecutor, Verifier
│   ├── level_4/              # SecurityCouncil orchestrator
│   ├── auth/                 # Authenticator, Permissions, WritFactory
│   ├── crypto/               # CouncilKeyManager
│   ├── config/               # CouncilSettings, RedisConfig
│   ├── llm/                  # LLM backends (Anthropic, OpenAI, NIM)
│   ├── nat/                  # NAT/MCP integration (functions, plugin)
│   ├── grpc/                 # gRPC proxy server + MCP client
│   ├── opa/                  # OPA policy-filter client
│   └── store/                # Redis + Postgres stores, migrations
├── trusted_executor_contract/ # Shared executor HTTP contract models
│   └── models.py             # Writ, request/response, health, descriptor, protocol
├── executors/
│   ├── common/               # Executor identity + signed evidence helpers
│   ├── docker/               # Local subprocess trusted executor
│   └── openshell/            # OpenShell trusted executor adapter
├── deploy/
│   ├── docker-compose.yml    # Redis + OPA + monitor + Council + executors
│   ├── Dockerfile            # Council container
│   ├── Dockerfile.executor   # Executor sandbox container (kubectl included)
│   ├── Dockerfile.openshell  # Local OpenShell-compatible runtime container
│   ├── dynamic-config/       # Runtime-mounted council catalog/profile copies
│   ├── executor.yaml         # Legacy Docker executor config (restrictive)
│   ├── executor-dev.yaml  # dev executor config
│   └── executor-openshell.yaml  # OpenShell executor config
├── configs/
│   ├── council.yaml          # Council configuration
│   └── council_nat_workflow.yaml  # NAT MCP workflow
├── scripts/
│   ├── agent_accounts.py     # Manage agent accounts in Redis
│   ├── seed_accounts.py      # Seed local demo accounts
│   └── test_live_e2e.py      # Live E2E test script
└── tests/                    # Core package tests
```

---
