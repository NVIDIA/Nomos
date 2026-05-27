import shutil
import subprocess

import pytest


def test_default_opa_policy_rego_tests_pass():
    opa = shutil.which("opa")
    if opa is None:
        pytest.skip("opa binary is not installed")

    result = subprocess.run(
        [opa, "test", "policies", "--fail-on-empty"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
