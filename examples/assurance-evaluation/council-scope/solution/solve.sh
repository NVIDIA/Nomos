#!/bin/sh
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
set -eu
python - <<'PY'
import json
from pathlib import Path
requests = json.loads(Path('/app/requests.json').read_text())
Path('/app/decisions.json').write_text(json.dumps({
    r['id']: 'approve' if r.get('target_environment') == 'staging' else 'deny'
    for r in requests
}))
PY
