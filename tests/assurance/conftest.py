# SPDX-License-Identifier: Apache-2.0
"""Use the optional pinned checkout provisioned by the PoC bootstrap command."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPASS = Path(os.environ.get("COMPASS_ROOT", ROOT / ".assurance-poc/upstream/compass"))
if COMPASS.is_dir():
    sys.path[:0] = [str(COMPASS / "src"), str(COMPASS / "packages/trace-ingest/src")]
