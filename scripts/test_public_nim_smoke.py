# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Run bounded model checks through the public NVIDIA API Catalog.

This manual smoke check consumes live API quota. Run it when changing the
public models, endpoint, backend configuration, or council response parsing:

    NVIDIA_API_KEY=... uv run python scripts/test_public_nim_smoke.py

To select another council config:

    NVIDIA_API_KEY=... uv run python scripts/test_public_nim_smoke.py --config path/to/council.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from nomos.config.settings import load_config
from nomos.level_0.types import Intent
from nomos.llm.council_agent import CouncilMemberAgent, create_backend

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PUBLIC_COUNCIL_CONFIG = ROOT / "configs/council.yaml"


async def run_smoke(config: Path) -> int:
    settings = load_config(str(config))
    if not settings.members:
        print("FAIL: council config defines no members", file=sys.stderr)
        return 1

    async def evaluate_member(member_config):
        backend_config = settings.llm_backends[member_config.backend].model_copy(
            update={"sdk_max_retries": 0}
        )
        agent = CouncilMemberAgent(
            member_config=member_config,
            backend=create_backend(backend_config),
            temperature=backend_config.temperature,
            max_tokens=backend_config.max_tokens,
            max_retries=0,
        )
        intent = Intent.create(
            agent_id="public-model-smoke",
            action_type="system.info",
            description=("Print the operating system name for a live model compatibility check."),
            target_resource="stdout",
            command="/usr/bin/uname -s",
            working_directory="/workspace",
            justification=(
                "Verify the configured public model can evaluate an exact Nomos request."
            ),
            blast_radius=("Read-only system metadata is written to stdout; no persistent changes."),
        )
        evaluation = await agent.evaluate_intent(intent)
        return member_config, backend_config, evaluation

    failures: list[str] = []
    for configured_member in settings.members:
        configured_backend = settings.llm_backends[configured_member.backend]
        print(
            f"CHECK: {configured_member.id} ({configured_backend.model})",
            flush=True,
        )
        member_config, backend_config, evaluation = await evaluate_member(configured_member)
        if "LLM evaluation failure" in evaluation.risk_concerns:
            failures.append(f"{member_config.id} ({backend_config.model}): {evaluation.reasoning}")
            continue
        print(
            f"PASS: {member_config.id} ({backend_config.model}) "
            f"returned decision={evaluation.decision}"
        )

    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
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
    if not os.environ.get("NVIDIA_API_KEY"):
        print(
            "NVIDIA_API_KEY is required; this command consumes live API quota.",
            file=sys.stderr,
        )
        return 2
    return asyncio.run(run_smoke(args.config))


if __name__ == "__main__":
    raise SystemExit(main())
