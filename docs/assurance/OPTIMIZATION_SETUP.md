<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Set up the council optimization walkthrough

Use a separate checkout/worktree for evaluation. Keep the deployed dashboard's
configuration in its own terminal. The council adapter rejects ambient
`COUNCIL_*` settings and creates disposable local identities, not deployed state.

## Install the pinned toolchain

From this branch's repository root:

```bash
uv sync --python 3.13 --frozen --extra all --extra assurance-compass
source .venv/bin/activate
export PYTHONPATH="$PWD"
umask 077
export EVAL_ROOT="$PWD/.runs/nomos-optimization/walkthrough-01"
export AUTHORING_ROOT="$PWD/.eval-author/walkthrough-01"
export DEVELOPMENT_HANDOFF="$EVAL_ROOT/development-handoff"
export CANDIDATE_OUTPUT="$PWD/.eval-author/walkthrough-01-candidate"
export EVAL_AUTHOR_ROOT=/tmp/nomos-eval-author
export HARBOR_PYTHON=/tmp/nomos-harbor/bin/python
mkdir -p "$EVAL_ROOT" "$AUTHORING_ROOT"
```

Use a new root per experiment. Preserve previous attempts and budgets. These
private directories are ignored; downloaded evidence and model interactions
still need review before sharing.

For missing tool checkouts/environments only:

```bash
git clone https://github.com/NVIDIA-NeMo/eval-author.git "$EVAL_AUTHOR_ROOT"
git -C "$EVAL_AUTHOR_ROOT" checkout 20f95a0a010d4905c13065678af51970404910c9
uv venv --python 3.13 /tmp/nomos-harbor
uv pip install --python "$HARBOR_PYTHON" -r evals/nomos/requirements.txt
```

For existing paths, inspect the revision and clean status instead of overwriting.
The Compass extra pins `518ad41d583e259205fb89623cb035ae3904f3f8` and Harbor
requirements pin 0.23.0. Check Docker and Compose/buildx are available:

```bash
git -C "$EVAL_AUTHOR_ROOT" rev-parse HEAD
git -C "$EVAL_AUTHOR_ROOT" status --short
"$HARBOR_PYTHON" -c 'from importlib.metadata import version; print(version("harbor"))'
docker info
docker compose version
docker buildx version
```

Install the complete pinned Eval Author skill folders into a fresh project's
`.agents/skills/` using its documented manual-copy method. Do not overwrite an
existing installation silently. For a fresh destination:

```bash
(
  set -eu
  test ! -e .agents/skills
  test ! -L .agents/skills
  mkdir -p .agents
  cp -R "$EVAL_AUTHOR_ROOT/skills" .agents/skills
)
```

If other skills are already installed, compare their identities and copy only
missing complete folders. Start a new coding-agent session and ask it to locate
`eval-author`, report its absolute path/pin, read its instructions, and confirm
supporting skills/tools are accessible. A cloned repository alone does not make
skills discoverable. Keep local skill installations out of the proposed change.

## Open a dashboard with authoring exports enabled

For an existing real council deployment follow [Compose/Helm setup](DEPLOYMENT.md)
and enable **`ASSURANCE_EVALUATION_EXPORT_ENABLED=true`** (Helm:
`assurance.evaluationExportEnabled: true`). Authenticate with operator Basic
credentials. The separate audit-export flag does not enable authoring bundles.

If you need a synthetic local dashboard for learning, follow
[the isolated fixture deployment](EVALUATION.md#isolated-fixture-deployment).
After deploying the current image to your chosen existing Kind cluster, enable
the separate rich export and forward the observer service:

```bash
kubectl --context kind-assurance-demo -n nomos-assurance-demo set env deployment/observer ASSURANCE_EVALUATION_EXPORT_ENABLED=true
kubectl --context kind-assurance-demo -n nomos-assurance-demo rollout status deployment/observer --timeout=180s
kubectl --context kind-assurance-demo -n nomos-assurance-demo port-forward --address 127.0.0.1 service/observer 9910:8080
```

These commands use the example cluster `assurance-demo`; substitute yours
consistently. Open `http://127.0.0.1:9910`; local credentials are in the private
`.assurance-local/access.env`. Synthetic findings remain labeled synthetic. The
fixture source does not establish real council quality or a client conversation.
Review exported context before deciding whether a finding supports this adapter.

Set the absolute path of the browser download in your evaluation terminal:

```bash
export BUNDLE_ZIP="$HOME/Downloads/nomos-evaluation-authoring.zip"
```

## Build and record the execution image

```bash
docker build -f evals/nomos/Dockerfile -t nomos-evaluation:walkthrough .
export EVAL_IMAGE=$(docker image inspect nomos-evaluation:walkthrough --format '{{.Id}}')
printf '%s\n' "$EVAL_IMAGE"
```

Give the author/evaluator that immutable image ID for `versions.runtime_image`,
your approved OpenAI-compatible endpoint/model, and the actual source revision.
Make `NOMOS_EVAL_API_KEY` available privately in the evaluation shell using your
approved credential mechanism. Do not put credentials in prompts or the manifest.
No provider route is assumed to remain accessible because it worked historically.

The four-case example reserves 24 council calls (four cases × two arms × three
repetitions). Its experiment has `total_seconds=7200`: Harbor reserves up to 120
seconds of setup/verification/collection in addition to each 120-second worker
window. Shared budget creation is delayed until execution. No silent budget
extension, error refund, or automatic model retry is part of the walkthrough.

## Native verifier control invocation

The evaluator renders each reviewed task with
`evals.nomos.harbor_bridge.prepare_task(directory, task, image)`. It writes
reviewed control answers privately, using `status=complete`, `layer=council`,
the expected/opposite decision, and an applicable valid correction. NOP is `{}`.
Use the same independent `grading.py`; do not replace it with a control-specific
grader. For each actual task/control, show and execute:

```bash
"$HARBOR_PYTHON" -m evals.nomos.harbor_driver \
  /absolute/rendered/task /absolute/new/control-output /absolute/control-answer.json control
```

The final `control` selects `ControlAgent`, which uploads a predetermined answer
instead of invoking a council model. Inspect `driver-result.json` for
`exception_info=null` and `verifier_result.rewards.reward`. Reference/valid
alternative earn 1, empty/wrong answers earn 0. Approve-all/deny-all rewards depend
on the case and must fail opposite-action cases. Repeat for every reviewed case
and record revision-bound evidence with the pinned Eval Author task-evidence tools.
Native task files contain verifier labels: keep final-task controls private.

## Optional model-backed diagnosis

The main guide's `analyze` command uses offline Compass signals and explicitly
skips inference. For model-backed ETHOS/grader-failure diagnosis, review a private
non-secret `diagnosis-config.json` using `InferenceConfig` fields, for example:

```json
{
  "model": "openai/<approved-model>",
  "api_base": "https://<approved-provider>/v1",
  "max_calls": 20,
  "max_tokens": 4096,
  "request_timeout": 120
}
```

Supply `NOMOS_EVAL_ANALYSIS_API_KEY` privately, then use the **same existing**
shared budget as council trials:

```bash
python -m evals.nomos analyze "$EVAL_ROOT/experiment" --split development \
  --inference-config "$EVAL_ROOT/diagnosis-config.json" --budget "$EVAL_ROOT/budget.json"
```

Read the analysis status, attempted requests, skipped signals, and errors in
`compass.json`. Preserve failed diagnosis attempts. A normal process exit or an
empty advisory finding list does not establish a successful diagnosis or prove
correctness. Review independent scores before interpreting recommendations.

## Troubleshooting

- **Cannot export:** confirm the rich-export flag, operator credentials, selected
  findings and preview completeness. An audit download is a different artifact.
- **Frozen identity changed:** retain the old experiment and start a new one after
  reviewing code/runtime changes; do not edit its recorded hashes.
- **Harbor failure:** inspect `harbor.log`, `driver-result.json`, worker evidence,
  Docker/Compose availability and the shared budget. Keep errors in accounting.
- **Perfect baseline / no credible candidate:** report that outcome. Do not weaken
  the instruction, labels, or required gain to create a demonstration win.
