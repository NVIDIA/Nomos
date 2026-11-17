# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""scripts/system_one_filter_validate.sh, run against a kubectl that records its calls."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
from pathlib import Path, PurePosixPath
from types import ModuleType, SimpleNamespace

import pytest
import yaml

from nomos.level_0.types import Attestation, AttestationVector

ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", ROOT / "scripts" / name)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def _deployed_profile(tmp_path: Path) -> Path:
    """A council profile whose system one block is the shipped one, references and all."""
    shipped = yaml.safe_load((ROOT / "configs" / "council.yaml").read_text())
    profile = tmp_path / "council.yaml"
    profile.write_text(
        yaml.safe_dump(
            {
                "council_id": "council-prod",
                "members": [],
                "redis": {
                    "password": "test-redis-password",  # pragma: allowlist secret
                    "account_integrity_key": "test-integrity-key",  # pragma: allowlist secret
                },
                "nat": {"host": "127.0.0.1"},
                "system_one": shipped["system_one"],
            }
        )
    )
    return profile


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        (
            {
                "COUNCIL_SYSTEM_ONE_PROVIDER": "external",
                "COUNCIL_SYSTEM_ONE_BASE_URL": "https://system-one.internal.example",
                "COUNCIL_SYSTEM_ONE_MODEL": "example-model-1.2.0",
                "SYSTEM_ONE_API_KEY": "external-key",  # pragma: allowlist secret
                "NVIDIA_API_KEY": "other-key",  # pragma: allowlist secret
            },
            (
                "external",
                "https://system-one.internal.example",
                "example-model-1.2.0",
                "external-key",
            ),
        ),
        (
            {"NVIDIA_API_KEY": "nemotron-key"},  # pragma: allowlist secret
            ("nemotron", None, None, "nemotron-key"),
        ),
    ],
    ids=["external", "nemotron"],
)
def test_probe_evaluates_against_the_deployed_system_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, environment, expected
) -> None:
    """The probe asks the backend the council is configured with, with that backend's key.

    It used to build the default provider's settings, so an external deployment
    was probed against the wrong endpoint, or not at all without that key.
    """
    for name in (
        "COUNCIL_SYSTEM_ONE_PROVIDER",
        "COUNCIL_SYSTEM_ONE_BASE_URL",
        "COUNCIL_SYSTEM_ONE_MODEL",
        "SYSTEM_ONE_API_KEY",
        "NVIDIA_API_KEY",
        "NOMOS_COUNCIL_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    catalog = ROOT / "configs" / "attestation_questions.yaml"
    monkeypatch.setenv("COUNCIL_SYSTEM_ONE_CATALOG_PATH", str(catalog))
    monkeypatch.setenv("COUNCIL_CATALOG_PATH", str(_deployed_profile(tmp_path)))
    probe = _load_script("system_one_filter_probe.py")

    configs = []

    class _RecordingClient:
        catalog = SimpleNamespace(version="test", questions=[])

        def __init__(self, config) -> None:
            configs.append(config)

        async def evaluate(self, intent) -> AttestationVector:
            return AttestationVector(
                model_version="example-model-1.2.0",
                catalog_version="test",
                attestations=[
                    Attestation(
                        question_id=question_id,
                        category="c",
                        qtype="noul",
                        value=0.9,
                        confidence=0.8,
                    )
                    for question_id in ("prompt_injection", "credential_exfiltration")
                ],
            )

    monkeypatch.setattr(probe, "SystemOneClient", _RecordingClient)

    assert asyncio.run(probe.main()) == 0
    (config,) = configs
    provider, base_url, model, key = expected
    assert config.provider == provider
    assert config.api_key == key
    assert config.catalog_path == str(catalog)
    if base_url is not None:
        assert (config.base_url, config.model_version) == (base_url, model)
