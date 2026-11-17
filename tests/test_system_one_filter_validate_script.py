# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""scripts/system_one_filter_validate.sh, run against a kubectl that records its calls."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]


def _kubectl_calls(tmp_path: Path) -> list[list[str]]:
    calls = tmp_path / "calls.log"
    stub = tmp_path / "kubectl"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$KUBECTL_CALLS"\n'
        'case " $* " in\n'
        '  *" get pods "*) printf "council-0" ;;\n'
        '  *" mktemp "*) printf "/tmp/tmp.created\\n" ;;\n'
        "esac\n"
    )
    stub.chmod(0o755)
    env_file = tmp_path / ".env"
    env_file.write_text("NOMOS_AGENT_ID=agent-test\n")

    subprocess.run(
        [str(ROOT / "scripts/system_one_filter_validate.sh")],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "KUBECTL_CALLS": str(calls),
            "ENV_FILE": str(env_file),
        },
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )
    return [line.split() for line in calls.read_text().splitlines()]


def _copied(calls: list[list[str]]) -> dict[str, PurePosixPath]:
    """Local file name -> path it was copied to in the pod."""
    copied = {}
    for args in calls:
        if "cp" in args:
            source, target = args[args.index("cp") + 1 : args.index("cp") + 3]
            copied[Path(source).name] = PurePosixPath(target.split(":", 1)[1])
    return copied


def test_tier_two_script_is_copied_with_the_helper_it_imports(tmp_path: Path) -> None:
    """The council image does not ship scripts/council_test_helpers.py.

    system_one_filter_e2e.py imports it as a sibling module, so it has to be copied
    into the same directory or Tier 2 stops on the import.
    """
    source = (ROOT / "scripts/system_one_filter_e2e.py").read_text()
    assert "from council_test_helpers import" in source

    copied = _copied(_kubectl_calls(tmp_path))

    assert "council_test_helpers.py" in copied
    helper = copied["council_test_helpers.py"]
    assert helper.name == "council_test_helpers.py"
    assert helper.parent == copied["system_one_filter_e2e.py"].parent


def test_probe_scripts_run_from_a_directory_created_in_the_pod(tmp_path: Path) -> None:
    """Nothing is written to, or run from, a fixed path under /tmp in the pod."""
    calls = _kubectl_calls(tmp_path)
    created = PurePosixPath("/tmp/tmp.created")  # what the recording kubectl's mktemp returns

    copied = _copied(calls)
    assert {path.parent for path in copied.values()} == {created}

    executed = [args[args.index("python") + 1] for args in calls if "python" in args]
    assert executed == [
        str(copied["system_one_filter_probe.py"]),
        str(copied["system_one_filter_e2e.py"]),
    ]

    assert calls[-1][-3:] == ["rm", "-rf", str(created)]
