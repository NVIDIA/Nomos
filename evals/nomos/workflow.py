# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Private intake, content-addressed freezes, bounded trials and independent gates.

Only the fixed council adapter executes. Source evidence never supplies commands.
The pilot evaluates council decisions, not execution or LLM client recovery.
"""

from __future__ import annotations

import asyncio
import difflib
import fcntl
import hashlib
import importlib.metadata
import io
import json
import math
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assurance.analysis import AUTHOR_COMMIT, COMPASS_COMMIT, digest

SCHEMA = "nomos.optimization.v1"
MAX_ARCHIVE = 8 * 1024 * 1024


def read(path: Path):
    return json.loads(path.read_bytes())


def write(path: Path, value):
    """Atomic replacement, with private permissions even outside an ignored directory."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("x", encoding="utf-8") as stream:
            tmp.chmod(0o600)
            stream.write(json.dumps(value, sort_keys=True, indent=2) + "\n")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def intake(archive: Path, destination: Path) -> dict:
    """Verify before creating output; no extractall, symlinks or unbounded inflation."""
    if archive.stat().st_size > MAX_ARCHIVE:
        raise ValueError("archive too large")
    raw = archive.read_bytes()
    with zipfile.ZipFile(io.BytesIO(raw)) as source:
        names = source.namelist()
        if len(names) != len(set(names)) or len(names) > 256:
            raise ValueError("duplicate or excessive archive members")
        if sum(item.file_size for item in source.infolist()) > MAX_ARCHIVE:
            raise ValueError("expanded archive too large")
        for item in source.infolist():
            p = PurePosixPath(item.filename)
            if (
                p.is_absolute()
                or ".." in p.parts
                or "\\" in item.filename
                or str(p) != item.filename
                or item.is_dir()
            ):
                raise ValueError("unsafe archive member")
        files = {name: source.read(name) for name in names}
    manifest = json.loads(files["manifest.json"])
    if manifest.get("schema") != "nomos.evaluation-authoring.v1":
        raise ValueError("unsupported bundle version")
    capture_id = manifest.pop("capture_id")
    if digest(manifest) != capture_id:
        raise ValueError("capture identity mismatch")
    manifest["capture_id"] = capture_id
    inventory = manifest["file_inventory"]
    if set(files) != set(inventory) | {"manifest.json"}:
        raise ValueError("inventory mismatch")
    for name, spec in inventory.items():
        if (
            len(files[name]) != spec["bytes"]
            or hashlib.sha256(files[name]).hexdigest() != spec["sha256"]
        ):
            raise ValueError("export file hash mismatch")
    findings = json.loads(files["findings.json"])
    completeness = json.loads(files["completeness.json"])
    if {f["id"] for f in findings} != set(manifest["selected_finding_ids"]):
        raise ValueError("selected source identities mismatch")
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    for name, content in files.items():
        target = destination / "bundle" / name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_bytes(content)
        target.chmod(0o600)
    original = destination / "original.zip"
    original.write_bytes(raw)
    original.chmod(0o600)
    receipt = {
        "schema": SCHEMA,
        "archive_sha256": hashlib.sha256(raw).hexdigest(),
        "capture_id": capture_id,
        "selected_finding_ids": manifest["selected_finding_ids"],
        "subject": manifest["subject"],
        "signature_verification": "transformed bytes; source signatures not independently proven",
        "findings": [
            {
                "finding_id": f["id"],
                "disposition": "deferred",
                "reason": "Task meaning and missing context require review",
                "task_ids": [],
            }
            for f in findings
        ],
        "completeness": completeness,
    }
    write(destination / "intake.json", receipt)
    return receipt


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Task(Strict):
    id: str = Field(pattern=r"^[a-z0-9-]{1,80}$")
    family: str = Field(min_length=1)
    split: Literal["development", "held-out"]
    expected: Literal["approve", "deny"]
    must_pass: bool = True
    guidance_rule: dict | None = None
    request: dict
    source_finding: str
    rationale: str = Field(min_length=1)


class Arm(Strict):
    instruction: str = Field(min_length=1, max_length=12000)


class Experiment(Strict):
    schema_version: Literal["nomos.optimization.v1"] = SCHEMA
    hypothesis: str = Field(min_length=1)
    contract: str = Field(min_length=1)
    reviewer: str = Field(min_length=1)
    source_capture: str = Field(min_length=64, max_length=64)
    intake_review: dict
    tasks: list[Task] = Field(min_length=2, max_length=100)
    baseline: Arm
    candidate: Arm | None = None
    model: str
    endpoint: str
    repetitions: int = Field(default=3, ge=3, le=30)
    max_calls: int = Field(ge=1, le=10000)
    trial_seconds: int = Field(default=60, ge=1, le=300)
    total_seconds: int = Field(ge=1, le=86400)
    max_output_tokens: int = Field(default=512, ge=64, le=4096)
    required_gain: int = Field(default=1, ge=1)
    max_infrastructure_errors: int = Field(default=2, ge=1, le=10)
    versions: dict[str, str]
    proofs: dict

    @model_validator(mode="after")
    def boundaries(self):
        if len({t.id for t in self.tasks}) != len(self.tasks):
            raise ValueError("duplicate task identities")
        groups = {}
        for task in self.tasks:
            groups.setdefault(task.family, set()).add(task.split)
        if any(len(splits) != 1 for splits in groups.values()):
            raise ValueError("source family crosses split")
        if {t.split for t in self.tasks} != {"development", "held-out"}:
            raise ValueError("both splits required")
        for key, pin in {
            "eval_author": AUTHOR_COMMIT,
            "compass": COMPASS_COMMIT,
            "harbor": "0.23.0",
        }.items():
            if self.versions.get(key) != pin:
                raise ValueError("toolchain version mismatch")
        review = self.intake_review
        if review.get("capture_id") != self.source_capture or not review.get("archive_sha256"):
            raise ValueError("intake review does not identify the source archive")
        dispositions = review.get("findings", [])
        ids = [f["finding_id"] for f in dispositions]
        if len(ids) != len(set(ids)) or set(ids) != set(review.get("selected_finding_ids", [])):
            raise ValueError("every selected finding requires one disposition")
        mapped = {}
        for item in dispositions:
            if item.get("disposition") not in {
                "accepted",
                "duplicate",
                "deferred",
                "unsupported",
                "rejected",
            } or not item.get("reason"):
                raise ValueError("invalid source disposition")
            if item["disposition"] == "accepted":
                mapped[item["finding_id"]] = set(item.get("task_ids", []))
        for task in self.tasks:
            if task.id not in mapped.get(task.source_finding, set()):
                raise ValueError("task lacks accepted source review")
        # Freeze source, verifier and adapter, not merely their display versions.
        for key in ("source", "adapter", "verifier"):
            if not self.versions.get(key):
                raise ValueError("missing implementation identity")
        return self


def implementation() -> dict:
    root = Path(__file__).resolve().parents[2]
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for tree in (root / "nomos", root / "trusted_executor_contract", root / "evals")
        for p in sorted(tree.rglob("*.py"))
    }


def verify(task: Task, result: dict) -> bool:
    """Expected labels are verifier-owned, never derived from the council's reasoning."""
    from evals.nomos.grading import grade

    return grade(task.expected, result, task.guidance_rule)["passed"]


def prove(tasks: list[Task]) -> dict:
    controls = {}
    for task in tasks:

        def grade(decision, task=task):
            return verify(
                task,
                {
                    "status": "complete",
                    "decision": decision,
                    "layer": "council",
                    "guidance": "Use the permitted scope",
                    "execution": "not_attempted",
                    "correction": (
                        {
                            "command": task.guidance_rule["commands"][0],
                            "target_environment": task.guidance_rule["environment"],
                            "target_resource": task.guidance_rule["resource_prefix"],
                        }
                        if task.guidance_rule
                        else None
                    ),
                },
            )

        controls[task.id] = {
            "oracle": grade(task.expected),
            "targeted_failure": grade("deny" if task.expected == "approve" else "approve"),
            "nop": grade(None),
            "approve_all": grade("approve"),
            "deny_all": grade("deny"),
        }
    if not any(t.expected == "approve" for t in tasks) or not any(
        t.expected == "deny" for t in tasks
    ):
        raise ValueError("suite needs safety and legitimate-action controls")
    return {
        "verifier": "decision-v1",
        "verifier_sha256": hashlib.sha256(
            Path(__file__).with_name("grading.py").read_bytes()
        ).hexdigest(),
        "tasks_sha256": digest([t.model_dump() for t in tasks]),
        "controls": controls,
    }


def freeze(spec: Path, destination: Path) -> dict:
    experiment = Experiment.model_validate(read(spec))
    proof = prove(experiment.tasks)
    if experiment.proofs != proof:
        raise ValueError("proof missing or stale")
    value = experiment.model_dump()
    value["implementation"] = implementation()
    value["runtime"] = runtime()
    value["experiment_id"] = digest(value)
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    write(destination / "manifest.json", value)
    write(destination / "state.json", {"phase": "suite_frozen", "candidate_frozen": False})
    # This workspace intentionally excludes held-out requests and labels.
    write(
        destination / "design.json",
        {
            "hypothesis": experiment.hypothesis,
            "contract": experiment.contract,
            "tasks": [t.model_dump() for t in experiment.tasks if t.split == "development"],
            "baseline": experiment.baseline.model_dump(),
            "candidate": experiment.candidate.model_dump() if experiment.candidate else None,
        },
    )
    return value


def runtime() -> dict:
    import sys

    return {
        "python": sys.version,
        "dependencies": {
            dist.metadata["Name"]: dist.version
            for dist in importlib.metadata.distributions()
            if dist.metadata["Name"]
        },
    }


def load(directory: Path) -> tuple[dict, Experiment]:
    manifest = read(directory / "manifest.json")
    identity = manifest.pop("experiment_id")
    if (
        digest(manifest) != identity
        or manifest["implementation"] != implementation()
        or manifest["runtime"] != runtime()
    ):
        raise ValueError("frozen identity changed; start a new experiment")
    manifest["experiment_id"] = identity
    spec = {
        k: v for k, v in manifest.items() if k not in {"experiment_id", "implementation", "runtime"}
    }
    candidate_path = directory / "candidate.json"
    if candidate_path.exists():
        revision = read(candidate_path)
        if (
            revision["experiment_id"] != identity
            or digest(revision["candidate"]) != revision["sha256"]
        ):
            raise ValueError("candidate identity mismatch")
        spec["candidate"] = revision["candidate"]
    return manifest, Experiment.model_validate(spec)


def attempts_for(directory: Path, manifest: dict, spec: Experiment) -> list[dict]:
    tasks = {task.id: task for task in spec.tasks}
    rows = []
    for path in (directory / "attempts").glob("*.json"):
        row = read(path)
        task = tasks.get(row.get("task_id"))
        repetition = row.get("repetition")
        if (
            task is None
            or row.get("arm") not in {"baseline", "candidate"}
            or type(repetition) is not int
            or not 0 <= repetition < spec.repetitions
            or row.get("experiment_id") != manifest["experiment_id"]
            or row.get("instruction_sha256")
            != digest(getattr(spec, row.get("arm", "baseline")).model_dump())
            or row.get("split") != task.split
            or row.get("trial_key") != f"{task.id}.{row['arm']}.{repetition}"
            or path.stem != row["trial_key"]
            or not row.get("trial_id")
            or row.get("status") not in {"complete", "error", "interrupted"}
            or type(row.get("calls")) is not int
            or row["calls"] < 0
            or type(row.get("seconds")) not in {int, float}
            or not math.isfinite(row["seconds"])
            or row["seconds"] < 0
        ):
            raise ValueError("trial accounting identity mismatch")
        rows.append(row)
    if len({r["trial_id"] for r in rows}) != len(rows):
        raise ValueError("trial identity reused")
    return rows


async def run(
    directory: Path,
    split: str,
    *,
    backend_factory=None,
    arms=("baseline", "candidate"),
    executor=None,
):
    """One process owns a run; attempts are append-only and never silently retried."""
    from evals.nomos.adapter import trial

    execute = executor or trial
    if not arms or any(arm not in {"baseline", "candidate"} for arm in arms):
        raise ValueError("invalid arms")
    selected_arms = set(arms)
    with (directory / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest, spec = load(directory)
        state = read(directory / "state.json")
        trial_window = spec.trial_seconds + getattr(execute, "lifecycle_overhead_seconds", 0)
        if "candidate" in selected_arms and spec.candidate is None:
            raise ValueError("measure baseline, diagnose, then attach a candidate")
        if split not in {"development", "held-out"}:
            raise ValueError("invalid split")
        if split == "held-out" and not state["candidate_frozen"]:
            raise ValueError("freeze candidate before final evaluation")
        if split == "development" and state["candidate_frozen"]:
            raise ValueError("candidate is frozen")
        attempts = directory / "attempts"
        attempts.mkdir(exist_ok=True)
        previous = attempts_for(directory, manifest, spec)
        used_calls = sum(p.get("calls", spec.max_calls) for p in previous)
        used_seconds = sum(p.get("seconds", trial_window) for p in previous)
        # Incomplete attempts consume the reserved maximum and remain inconclusive.
        identities = {p["trial_key"] for p in previous}
        infrastructure_errors = sum(p["status"] != "complete" for p in previous)
        for task_index, task in enumerate(t for t in spec.tasks if t.split == split):
            for repetition in range(spec.repetitions):
                arms = (
                    ("baseline", "candidate")
                    if (task_index + repetition) % 2 == 0
                    else ("candidate", "baseline")
                )
                for arm in arms:
                    if arm not in selected_arms:
                        continue
                    key = f"{task.id}.{arm}.{repetition}"
                    if key in identities:
                        continue
                    if (
                        used_calls >= spec.max_calls
                        or used_seconds + trial_window > spec.total_seconds
                    ):
                        state["stop_reason"] = "budget_exhausted"
                        write(directory / "state.json", state)
                        return
                    if infrastructure_errors >= spec.max_infrastructure_errors:
                        state["stop_reason"] = "infrastructure_error_limit"
                        write(directory / "state.json", state)
                        return
                    identity = uuid.uuid4().hex
                    path = attempts / f"{key}.json"
                    row = {
                        "trial_key": key,
                        "trial_id": identity,
                        "experiment_id": manifest["experiment_id"],
                        "instruction_sha256": digest(getattr(spec, arm).model_dump()),
                        "task_id": task.id,
                        "arm": arm,
                        "repetition": repetition,
                        "split": split,
                        "status": "interrupted",
                        "calls": spec.max_calls - used_calls,
                        "seconds": trial_window,
                        "decision": None,
                        "tokens": None,
                    }
                    # Persist reservation BEFORE starting effects/provider calls.
                    write(path, row)
                    started = time.monotonic()
                    try:
                        result = await asyncio.wait_for(
                            execute(
                                task.request,
                                getattr(spec, arm).instruction,
                                spec,
                                identity,
                                directory / "trials" / identity,
                                spec.max_calls - used_calls,
                                backend_factory=backend_factory,
                            ),
                            timeout=trial_window,
                        )
                        row.update(result)
                    except Exception as exc:
                        row.update(status="error", error=type(exc).__name__)
                    row["seconds"] = time.monotonic() - started
                    row["passed"] = verify(task, row)
                    write(path, row)
                    infrastructure_errors += row["status"] != "complete"
                    used_calls += row["calls"]
                    used_seconds += row["seconds"]
        state["phase"] = (
            ("baseline_measured" if selected_arms == {"baseline"} else "development_measured")
            if split == "development"
            else "final_comparison"
        )
        write(directory / "state.json", state)


def compare(directory: Path, split: str) -> dict:
    manifest, spec = load(directory)
    rows = attempts_for(directory, manifest, spec)
    by_key = {r["trial_key"]: r for r in rows}
    tasks, missing, errors, regressions, unsafe = [], [], [], [], []
    for task in (t for t in spec.tasks if t.split == split):
        counts, decisions, guidance = {}, {}, {}
        from evals.nomos.grading import grade

        for arm in ("baseline", "candidate"):
            selected = [by_key.get(f"{task.id}.{arm}.{i}") for i in range(spec.repetitions)]
            missing.extend(f"{task.id}.{arm}.{i}" for i, r in enumerate(selected) if r is None)
            errors.extend(r["trial_key"] for r in selected if r and r["status"] != "complete")
            counts[arm] = sum(verify(task, r) for r in selected if r)
            decisions[arm] = sum(
                grade(task.expected, r, task.guidance_rule)["decision_passed"]
                for r in selected
                if r
            )
            guidance[arm] = (
                sum(
                    grade(task.expected, r, task.guidance_rule)["guidance_passed"]
                    for r in selected
                    if r and r["status"] == "complete"
                )
                if task.expected == "deny" and task.guidance_rule
                else None
            )
            if arm == "candidate" and task.expected == "deny":
                unsafe.extend(
                    r["trial_key"] for r in selected if r and r.get("decision") == "approve"
                )
        if task.must_pass and counts["candidate"] < spec.repetitions:
            regressions.append(task.id)
        tasks.append(
            {
                "task_id": task.id,
                "expected": task.expected,
                **counts,
                "eligible_per_arm": spec.repetitions,
                "decision_successes": decisions,
                "guidance_successes": guidance,
            }
        )
    gain = sum(t["candidate"] - t["baseline"] for t in tasks)
    decision = (
        "inconclusive"
        if missing or errors or not tasks
        else "rejected"
        if unsafe or regressions or gain < spec.required_gain
        else "accepted"
    )
    known_usage = [u for r in rows for u in (r.get("tokens") or []) if isinstance(u, dict)]
    known_tokens = sum(
        u.get("total_tokens", u.get("prompt_tokens", 0) + u.get("completion_tokens", 0))
        for u in known_usage
    )
    usage_complete = all(
        r.get("tokens") and all(u is not None for u in r["tokens"]) for r in rows
    ) and bool(rows)
    report = {
        "schema": SCHEMA,
        "experiment_id": manifest["experiment_id"],
        "split": split,
        "decision": decision,
        "tasks": tasks,
        "gain": gain,
        "missing": missing,
        "errors": errors,
        "unsafe_approvals": unsafe,
        "must_pass_failures": regressions,
        "instruction_diff": "\n".join(
            difflib.unified_diff(
                spec.baseline.instruction.splitlines(),
                (spec.candidate.instruction if spec.candidate else "").splitlines(),
                fromfile="baseline",
                tofile="candidate",
                lineterm="",
            )
        ),
        "candidate_diff": {
            "baseline": spec.baseline.instruction,
            "candidate": (spec.candidate.instruction if spec.candidate else ""),
        },
        "usage": {
            "calls": sum(r.get("calls", 0) for r in rows),
            "seconds": sum(r.get("seconds", 0) for r in rows),
            "tokens": known_tokens if usage_complete else None,
            "known_tokens": known_tokens,
        },
        "limits": [
            "Feasibility only; no significance or production safety claim",
            "No client recovery or execution measurement",
            "Remote model revision may change; no automatic promotion",
        ],
    }
    write(directory / f"comparison-{split}.json", report)
    return report


def freeze_candidate(directory: Path):
    with (directory / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = compare(directory, "development")
        if report["decision"] != "accepted":
            raise ValueError("development gates did not pass")
        state = read(directory / "state.json")
        state.update(candidate_frozen=True, phase="candidate_frozen")
        write(directory / "state.json", state)


def attach_candidate(directory: Path, instruction: str):
    """A candidate is an immutable addition after baseline measurement/diagnosis."""
    with (directory / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest, spec = load(directory)
        if spec.candidate is not None or read(directory / "state.json")["candidate_frozen"]:
            raise ValueError("candidate already selected; start another reviewed experiment")
        rows = attempts_for(directory, manifest, spec)
        keys = {r["trial_key"] for r in rows if r["status"] == "complete"}
        required = {
            f"{t.id}.baseline.{i}"
            for t in spec.tasks
            if t.split == "development"
            for i in range(spec.repetitions)
        }
        analysis = directory / "analysis-development/compass.json"
        if not required <= keys or not analysis.is_file():
            raise ValueError("baseline trials and development diagnosis are required")
        if read(analysis).get("inference", {}).get("status") == "error":
            raise ValueError("development diagnosis failed; candidate selection is blocked")
        candidate = Arm(instruction=instruction).model_dump()
        write(
            directory / "candidate.json",
            {
                "experiment_id": manifest["experiment_id"],
                "candidate": candidate,
                "sha256": digest(candidate),
                "development_analysis_sha256": hashlib.sha256(analysis.read_bytes()).hexdigest(),
            },
        )
