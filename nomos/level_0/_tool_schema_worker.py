# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Static subprocess entry point: bounded JSON in, one-byte JSON status out.

Run only via the schema adapter. No caller module, pickle or executable payload
is loaded; -I ignores caller Python environment and current working directory.
"""

import json
import sys
from pathlib import Path

# Direct script execution under -I needs the trusted installed package root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from nomos.level_0.tool_schema import _checked_validator
from nomos.level_0.types.tool_call import (
    TOOL_ARGUMENTS_MAX_BYTES,
    canonical_tool_arguments,
)
from nomos.level_0.types.tool_definition import TOOL_SCHEMA_MAX_BYTES


def main() -> None:
    """Never return errors, schema text or argument data over the pipe."""
    limit = TOOL_SCHEMA_MAX_BYTES + TOOL_ARGUMENTS_MAX_BYTES + 3
    try:
        data = sys.stdin.buffer.read(limit + 1)
        if len(data) > limit:
            raise ValueError("oversized validation request")
        schema, arguments = json.loads(data)
        canonical_tool_arguments(arguments)
        _checked_validator(schema).validate(arguments)
        result = b"1"
    except Exception:
        # All validation/library/protocol failures deny; stderr is also discarded.
        result = b"0"
    sys.stdout.buffer.write(result)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
