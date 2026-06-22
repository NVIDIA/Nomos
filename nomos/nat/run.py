# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


"""
Standalone council runner.

Starts the council service without NAT, useful for:
  - Local development and testing
  - Environments where nvidia-nat is not installed
  - Direct Python integration

Usage:
    python -m nomos.nat.run --config configs/council.yaml
"""

import argparse
import asyncio
import json
import logging
import sys

from nomos.config.settings import load_config
from nomos.nat.functions import CouncilService, ProposeIntentInput

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def interactive_session(service: CouncilService) -> None:
    """Run an interactive session for submitting intents."""
    print("\n" + "=" * 60)
    print("Nomos Council - Interactive Mode")
    print(f"Council members: {[a.display_name for a in service.agents]}")
    print("=" * 60)
    print("\nType 'quit' to exit, 'pending' to list pending approvals.\n")

    while True:
        try:
            line = input("council> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            break

        if not line:
            continue
        if line.lower() == "quit":
            break
        if line.lower() == "pending":
            result = service.get_pending_approvals()
            print(json.dumps(result.model_dump(), indent=2))
            continue
        if line.lower().startswith("status "):
            from nomos.nat.functions import IntentStatusInput

            intent_id = line.split(" ", 1)[1].strip()
            result = service.check_intent_status(IntentStatusInput(intent_id=intent_id))  # type: ignore[assignment]
            print(json.dumps(result.model_dump(), indent=2))
            continue

        # Parse as JSON intent
        try:
            data = json.loads(line)
            input_data = ProposeIntentInput.model_validate(data)
        except json.JSONDecodeError:
            print("Input must be a JSON object. Example:")
            print(
                '  {"agent_id": "my-agent", "command": "/bin/ls -la /workspace", '
                '"description": "List files in the workspace directory", '
                '"target_resource": "/workspace", '
                '"justification": "Need to verify current workspace contents"}'
            )
            continue
        except Exception as e:
            print(f"Validation error: {e}")
            continue

        result = await service.propose_intent(input_data)  # type: ignore[assignment]
        print(json.dumps(result.model_dump(), indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Nomos Council Runner")
    parser.add_argument(
        "--config",
        "-c",
        required=True,
        help="Path to council YAML configuration file",
    )
    parser.add_argument(
        "--interactive",
        "-i",
        action="store_true",
        default=True,
        help="Run in interactive mode (default)",
    )
    args = parser.parse_args()

    try:
        settings = load_config(args.config)
    except Exception as e:
        print(f"Failed to load config: {e}", file=sys.stderr)
        sys.exit(1)

    service = CouncilService(settings)
    asyncio.run(service.init_redis())
    logger.info(
        "Council '%s' ready with %d members",
        settings.council_id,
        len(service.agents),
    )

    if args.interactive:
        asyncio.run(interactive_session(service))


if __name__ == "__main__":
    main()
