<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Test the OpenShell integration lab

First complete [lab setup](lab-setup.md). Run commands from
`integrations/openshell`, using a dedicated sandbox and destination. The tests
below check enforcement and forwarding, separately from model quality.

## Understand the two kinds of model fixture

The Rust [service tests](../tests/review.rs) use either an in-memory provider
returning fixed assessments, or temporary local HTTP servers returning prepared
model responses. They run with `cargo test --locked --all-targets`, without a
gateway or real model.

The standalone simulated model ([mock_decision_model.py](../tools/integration-lab/mock_decision_model.py))
is different: you run it as a process, point the real middleware at it, and send
requests through an actual OpenShell sandbox. It returns preset answers without
examining content. The middleware applies thresholds and OpenShell enforces
the decision. This tests the complete integration deterministically; it does
not measure real-model detection quality.

## Use the local simulated model

In an additional terminal:

```sh
python3 tools/integration-lab/mock_decision_model.py --threshold 0.7 --delay 6
```

Stop only your dedicated lab middleware process and restart it against the
fixture. The explicit CLI values override any real-model selection in your
environment, and `env -u` prevents sending a real model credential to the fixture:

```sh
env -u NOMOS_MODEL_TOKEN cargo run --locked --bin nomos-request-review -- \
  --model-endpoint http://127.0.0.1:18082/v1/systemone \
  --model-id nomos-lab-fixture --expected-model-id nomos-lab-fixture \
  --allow-http-model --bind 0.0.0.0:50051 --allow-insecure-lab
```

Keep the two example policy rules at threshold 0.7 with default yes-denial
direction. Model/evaluation/RPC budgets remain 4/4.5/5 seconds; six seconds
exceeds them for the timeout check. This does not modify policy or the gateway.
Do not run fixture-backed checks against middleware still pointing at a real
model; changing `/admin` would then have no effect on its decisions.

## Run the default checks

```sh
python3 tools/integration-lab/lab.py deterministic
```

The runner uses the selected default gateway and sandbox `nomos-review-lab`.
It runs 18 checks:

1. Ten modes cover below/equal/above-threshold scores, independent triggering of
   each disclosure rule, HTTP errors, malformed/missing answers and timeout.
2. Five invalid-input checks cover duplicate JSON keys, malformed JSON, invalid
   UTF-8, compression and a body exceeding the default body cap.
3. One larger valid body must arrive unchanged after one model call.
4. One permitted PUT must arrive unchanged, and one forbidden path must be
   rejected before a model call.

This middleware rejects duplicate keys and malformed/unsupported content;
these are input failures, not model-scored content denials. Oversized input can
be rejected by OpenShell before invoking the service. The helper checks the
observed workload error, absence of forwarding and model-call count.

For all cases, output records distinguish content denial, policy denial and
assessment failure. Read the [helper guide](../tools/integration-lab/README.md)
for all fixture modes, options and record fields.

## Check HTTP methods and other routes

With the default policy and fixture-backed middleware:

```sh
python3 tools/integration-lab/lab.py http-methods
```

Seven ordinary HTTP methods must allow, each with a JSON body, one model call
and exact destination capture. Two rejections cover an unsupported extension
method and forbidden `DELETE /requests`. The extension-method expectation is
OpenShell-version-specific: review any failure against your version rather than
interpreting it as a model error. HEAD is covered by destination tests; this
runner expects a JSON response body and does not classify HEAD.

For the separate multi-route policy:

```sh
openshell sandbox create \
  --name nomos-review-routes --from nomos-review-workload:lab \
  --policy examples/policy-multiple-routes.yaml \
  --no-auto-providers --no-tty --detach -- sleep infinity
python3 tools/integration-lab/lab.py multiple-routes --sandbox nomos-review-routes
```

Eight checks cover allow, content denial and invalid-assessment failure for
`POST /messages` and `PUT /documents/123`, plus unpermitted method/path denials.
The middleware itself has no hardcoded route restriction.

For an independent network-policy negative control:

```sh
openshell sandbox create \
  --name nomos-review-policy-control --from nomos-review-workload:lab \
  --policy examples/policy-network-control.yaml \
  --no-auto-providers --no-tty --detach -- sleep infinity
python3 tools/integration-lab/lab.py network-control --sandbox nomos-review-policy-control
```

That policy permits only `/different-route`; the helper attempts `/publish` and
requires a network-policy denial with no destination arrival or model call.

## Check your actual model separately

Stop the fixture-backed middleware and restart it with your actual HTTPS model
settings using [lab setup](lab-setup.md#5-start-the-middleware--terminal-b).
Before the sandbox checks, you can explicitly run the ignored connectivity test:

```sh
cargo test --locked --test live -- --ignored
```

This test calls the configured model endpoint using `NOMOS_MODEL_ENDPOINT`,
required `NOMOS_MODEL_ID`, optional `NOMOS_EXPECTED_MODEL_ID` and
`NOMOS_MODEL_TOKEN`. It checks response compatibility, not model accuracy.
It is skipped by the ordinary test suite and CI.
Then run:

```sh
python3 tools/integration-lab/lab.py live-tests
```

Five synthetic examples cover a public announcement, credential-safety advice,
a fictional populated credential, fictional confidential information and an
embedded instruction attempting to override review. The text and intended
labels are visible in [lab.py](../tools/integration-lab/lab.py). No real secrets
or private company information are included. The runner itself loads no model
credential; only the middleware calls the configured model.

These examples check intended behavior, not statistical accuracy. A fail-closed
model error does not count as a successful content detection. Inspect
[middleware logs](logging.md) for the actual scores and provider outcome.

## Outage and recovery

Stop your dedicated middleware, send the benign sandbox request from setup,
and verify OpenShell blocks forwarding under `on_error: fail_closed`. There
should be no new destination arrival. A stopped process cannot emit a review
event; inspect the OpenShell logs too. Restart the same middleware command and
repeat the benign request to verify recovery.

Run checks sequentially and keep other clients away from the destination.
Results append to ignored `tools/integration-lab/results.jsonl`; they are local
evidence, not files to publish. The helpers do not edit policy, switch model
providers, or deploy/restart services for you.
