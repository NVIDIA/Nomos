<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Integration lab helpers

These Python standard-library tools test services that the operator already
started. They do not load credentials, modify policy or start/stop other services.
Follow [lab setup](../../docs/lab-setup.md) and
[lab testing](../../docs/lab-testing.md) for the complete procedure.

| File | Purpose |
| --- | --- |
| [lab.py](lab.py) | Uses the OpenShell CLI to run curl inside an existing sandbox, then checks HTTP outcomes and destination evidence |
| [mock_decision_model.py](mock_decision_model.py) | Loopback-only simulated model returning preset SystemOne-compatible answers; no actual model or content analysis |
| [test_helpers.py](test_helpers.py) | Local unit tests of the runner's evidence checks and simulated model responses |

Run from `integrations/openshell`, using Python 3.11 or later:

```sh
python3 tools/integration-lab/mock_decision_model.py
```

The fixture listens on `127.0.0.1:18082`. Configure the middleware to use that
HTTP endpoint with the explicit model and expected response ID
`nomos-lab-fixture`, as described in [lab testing](../../docs/lab-testing.md#use-the-local-simulated-model).
Only the middleware calls this endpoint; sandbox publications go to the separate
[destination](../test-destination/README.md).

## Runner actions

After the sandbox, gateway, middleware and destination are running:

```sh
python3 tools/integration-lab/lab.py deterministic
```

The default sandbox is `nomos-review-lab`, and the runner follows the CLI's
selected default gateway. `--gateway NAME` and `--gateway-endpoint URL` explicitly
override it. Other options include `--sandbox`, `--destination-url`,
`--inspection-url`, `--mock-url`, `--threshold`, `--openshell` and `--results`.
Run `python3 tools/integration-lab/lab.py --help` for details.

| Action | Checks | Required configuration |
| --- | --- | --- |
| `deterministic` | 18: ten preset score/failure modes, five input rejections, larger valid body, permitted PUT and forbidden path | Example two-question policy, both default yes-denial rules at 0.7; simulated model |
| `http-methods` | Nine: seven ordinary methods, extension-method rejection and forbidden inspection path | Same policy and simulated model; extension-method result depends on OpenShell version |
| `multiple-routes` | Eight: allow/deny/error for each of two routes, plus method/path denials | Separate sandbox using [multi-route policy](../../examples/policy-multiple-routes.yaml); simulated model |
| `network-control` | One: `/publish` denied before a model call | Separate sandbox using [network-control policy](../../examples/policy-network-control.yaml); simulated model |
| `live-tests` | Five synthetic examples with intended allow/deny labels | Middleware explicitly restarted against your configured real model |

The runner distinguishes `policy_denied`, `middleware_denied` and
`middleware_failed`. A 403 or absent arrival alone cannot pass a content-denial
check. An allow requires one arrival with the expected method/path and unchanged
body bytes, length and SHA-256. Fixture-backed actions also check model-call
counts, so a network rejection cannot masquerade as content review.

`--threshold` describes the already loaded policy and fixture threshold. It does
not edit either. These packaged scenarios use `deny_when: "yes"` and 0.7;
arbitrary question catalogs, no-direction rules or different size/deadline
settings can change their expectations. The fixture must match the policy's
threshold, and its timeout delay must exceed the configured deadline.

## Simulated model modes

Only `POST /admin` changes the mode. `GET /admin` reports mode, threshold and
call count; `POST /v1/systemone` returns the selected answers and increments
the model-call count. A mode field in a model request does not change fixture
configuration. The initial mode is `low`.

| Mode | Response at the default threshold of 0.7 |
| --- | --- |
| `low` | All yes scores 0.1 |
| `below` | All yes scores 0.69 |
| `equals` | All yes scores 0.7 |
| `high` | All yes scores 0.99 |
| `credential-only` | `credential_disclosure` 0.7; other scores 0.1 |
| `confidential-only` | `confidential_disclosure` 0.7; other scores 0.1 |
| `http-error` | HTTP 503, even though its body contains low answers |
| `malformed` | HTTP 200 with invalid JSON |
| `missing` | Valid JSON with the first incoming question's answer omitted; remaining scores 0.1 |
| `timeout` | Six-second delay by default, then low answers if the caller still waits |

The two mixed-score modes require both example rule IDs. Only the named rule
reaches the threshold, verifying that either rule independently triggers denial.
The module-level docstring in [mock_decision_model.py](mock_decision_model.py)
includes complete request/response examples and each mode's purpose.

The fixture accepts model request bodies up to 64 KiB, independently of the
middleware's larger production-facing byte gates. It is a small-request test
fixture, not a model context-window emulator. Use the Rust transport tests for
full boundary coverage. With changed timeouts, start it with `--delay SECONDS`
large enough to exceed the middleware deadline.

## Results and local checks

Results append to ignored `tools/integration-lab/results.jsonl` by default.
Exit code 0 means all checks passed, 1 means an expectation failed, and 2 means
an infrastructure/configuration problem. Records include outcomes, counts,
timing and exact-body check results, not raw bodies, CLI stderr or credentials.

Use one runner at a time against a dedicated destination with no other callers.
The destination's counts include all clients and its default store retains 100
bodies. Inspect/export evidence before restarting, which clears the store.
No benchmark datasets or reporting tools are needed.

Check the helpers themselves without Docker, OpenShell or model credentials:

```sh
python3 -m unittest discover -s tools/integration-lab -v
```
