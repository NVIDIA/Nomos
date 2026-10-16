# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Run bounded checks against the model endpoints selected by a council config.

This manual smoke check consumes live API quota. Run it when changing the
public models, endpoint, backend configuration, or council response parsing:

    NVIDIA_API_KEY=... uv run python scripts/test_public_nim_smoke.py

To select another council config:

Export each API-key environment variable referenced by that config, then run:

    uv run python scripts/test_public_nim_smoke.py --config path/to/council.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import yaml  # type: ignore[import-untyped]

from nomos.config.settings import load_config
from nomos.level_0.types import Intent, PackagedFile
from nomos.llm.base import LLMBackend
from nomos.llm.council_agent import (
    DEFAULT_CODE_REVIEW_MAX_TOKENS,
    ChairmanAgent,
    CodeReviewAgent,
    CouncilMemberAgent,
    _load_json_object,
    create_backend,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PUBLIC_COUNCIL_CONFIG = ROOT / "configs/council.yaml"
ENV_REFERENCE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


class RecordingBackend(LLMBackend):
    """Delegate to the real backend while retaining evidence, never raw error text."""

    def __init__(self, backend):
        self.backend = backend
        self.responses = []
        self.calls = 0
        self.error = ""
        self.http_status = None

    async def complete(self, *args, **kwargs):
        self.calls += 1
        try:
            response = await self.backend.complete(*args, **kwargs)
        except asyncio.CancelledError:
            self.error = "DeadlineExceeded"
            raise
        except Exception as exc:
            self.error = type(exc).__name__
            self.http_status = getattr(exc, "status_code", None)
            raise
        self.responses.append(response)
        return response

    def provider_name(self):
        return self.backend.provider_name()


def smoke_intent(*, packaged=False):
    """Describe harmless work; the smoke never executes the command or file."""
    return Intent.create(
        agent_id="public-model-smoke",
        action_type="system.info",
        description="Print a fixed compatibility-check message." if packaged else "Print OS name.",
        target_resource="stdout",
        command="python3 smoke.py" if packaged else "/usr/bin/uname -s",
        working_directory="/workspace",
        justification="Verify the configured public model can evaluate an exact Nomos request.",
        blast_radius="Only a harmless message is written to stdout; no persistent changes.",
        packaged_files=[
            PackagedFile(
                path="smoke.py",
                content_base64=base64.b64encode(b'print("Nomos compatibility check")\n').decode(),
                description="Print a fixed message without imports, file I/O, or network access.",
            )
        ]
        if packaged
        else [],
    )


def safe_endpoint(backend_config):
    # Strip URL credentials and query/fragment tokens from evidence.
    from nomos.llm.council_agent import _backend_class

    url = backend_config.base_url or getattr(
        _backend_class(backend_config.provider), "DEFAULT_BASE_URL", ""
    )
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", ""))


def required_api_key_env_vars(config: Path) -> set[str]:
    """Return API-key environment references without resolving or printing values."""
    try:
        raw = yaml.safe_load(config.read_text())
    except (OSError, yaml.YAMLError):
        return set()
    if not isinstance(raw, dict) or not isinstance(raw.get("llm_backends"), dict):
        return set()

    required = set()
    for backend in raw["llm_backends"].values():
        if not isinstance(backend, dict):
            continue
        match = ENV_REFERENCE.fullmatch(str(backend.get("api_key", "")))
        if match:
            required.add(match.group(1))
    return required


async def run_smoke(config: Path) -> int:
    # SDK/agent logs can contain provider bodies or malformed model output.
    # Emit only our bounded metadata, and restore logging for callers/tests.
    prior_logging = logging.root.manager.disable
    logging.disable(sys.maxsize)
    try:
        return await _run_smoke(config)
    finally:
        logging.disable(prior_logging)


async def _run_smoke(config: Path) -> int:
    try:
        digest = hashlib.sha256(config.read_bytes()).hexdigest()
        settings = load_config(str(config))
    except Exception as exc:
        print(f"FAIL: config could not be loaded ({type(exc).__name__})", file=sys.stderr)
        return 1
    print(json.dumps({"config_sha256": digest}), flush=True)
    if not settings.members:
        print("FAIL: council config defines no members", file=sys.stderr)
        return 1

    roles = [(f"member:{member.id}", member.backend, member) for member in settings.members]
    for name in ("chairman", "code_review"):
        role_config = getattr(settings, name)
        if role_config.enabled:
            roles.append((name, role_config.backend, role_config))

    failures = 0
    for role, backend_id, role_config in roles:
        record = {"role": role, "backend": backend_id}
        recording = None
        start = time.monotonic()
        try:
            backend_config = settings.llm_backends[backend_id].model_copy(
                update={"sdk_max_retries": 0}
            )
            record.update(
                endpoint=safe_endpoint(backend_config), requested_model=backend_config.model
            )
            print("CHECK: " + json.dumps(record), flush=True)
            recording = RecordingBackend(create_backend(backend_config))
            kwargs = {
                "backend": recording,
                "temperature": backend_config.temperature,
                "max_tokens": backend_config.max_tokens,
                "max_retries": 0,
                "retry_budget_seconds": settings.llm_retry_budget_seconds,
                "max_retry_after_seconds": settings.llm_max_retry_after_seconds,
            }
            if role == "chairman":
                agent = ChairmanAgent(
                    system_prompt=role_config.system_prompt_override,
                    allow_quorum=role_config.allow_quorum,
                    **kwargs,
                )
                result = await agent.decide_threshold(smoke_intent())
                field, allowed = "threshold", {"quorum", "unanimous"}
            elif role == "code_review":
                # Match the deployed reviewer's minimum output budget.
                kwargs["max_tokens"] = max(
                    backend_config.max_tokens, DEFAULT_CODE_REVIEW_MAX_TOKENS
                )
                agent = CodeReviewAgent(
                    system_prompt=role_config.system_prompt_override,
                    display_name=role_config.display_name,
                    max_file_bytes=role_config.max_file_bytes,
                    **kwargs,
                )
                result = await agent.review_intent_files(smoke_intent(packaged=True))
                field, allowed = "decision", {"approve", "deny"}
            else:
                agent = CouncilMemberAgent(member_config=role_config, **kwargs)
                result = await agent.evaluate_intent(smoke_intent())
                field, allowed = "decision", {"approve", "deny"}

            if recording.error:
                record["error"] = recording.error
            elif recording.calls != 1 or len(recording.responses) != 1:
                record["error"] = "ExpectedOneInferenceResponse"
            elif result.timing.get("attempts") != 1:
                # timing["attempts"] is set by the agent only after production
                # parsing succeeds. Fail-closed results carry at most token counts.
                record["error"] = "ParseFailureOrFallback"
            else:
                data = _load_json_object(recording.responses[0].content)
                # Production parsers normalize unknown decisions/thresholds;
                # an endpoint compatibility check must not count those as valid.
                if (
                    str(data.get(field, "")).lower().strip() not in allowed
                    or not str(data.get("reasoning") or "").strip()
                ):
                    record["error"] = "InvalidResponse"
                else:
                    record[field] = getattr(result, field)
        except Exception as exc:
            record["error"] = type(exc).__name__
        if recording is not None:
            if recording.responses:
                record["returned_model"] = recording.responses[-1].model
            if recording.http_status is not None:
                record["http_status"] = recording.http_status
            record["inference_calls"] = recording.calls
        record["elapsed_seconds"] = round(time.monotonic() - start, 3)
        failed = "error" in record
        failures += failed
        print(
            ("FAIL: " if failed else "PASS: ") + json.dumps(record),
            file=sys.stderr if failed else sys.stdout,
            flush=True,
        )
    return 1 if failures else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_PUBLIC_COUNCIL_CONFIG,
        help="Council YAML to exercise (default: configs/council.yaml).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    missing = sorted(
        name for name in required_api_key_env_vars(args.config) if not os.environ.get(name)
    )
    if missing:
        print(
            "Missing API key environment variables referenced by the selected config: "
            + ", ".join(missing)
            + ". This command consumes live API quota.",
            file=sys.stderr,
        )
        return 2
    return asyncio.run(run_smoke(args.config))


if __name__ == "__main__":
    raise SystemExit(main())
