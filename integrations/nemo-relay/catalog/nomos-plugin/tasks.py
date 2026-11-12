# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Build, test, and package the Nomos worker from its pinned remote source."""

import argparse
import os
import shutil
import sys
import tomllib

sys.path.insert(0, os.environ["REPO_DIR"])
from scripts.tasks import Context, run, write_attributions, write_runtime_manifest

PACKAGE = "nemo_relay_nomos"
# Everything Relay needs to install the worker; the runtime manifest is rewritten with a fresh digest.
SOURCE_FILES = ["pyproject.toml", "uv.lock", "LICENSE", "NOTICE", "README.md"]


def uv(ctx: Context, *args):
    python = ctx.release["toolchains"]["python"]
    run(["uv", *args[:1], "--locked", "--python", python, "--project", ctx.source, *args[1:]], cwd=ctx.source)


def build(ctx: Context):
    uv(ctx, "sync", "--group", "test")
    run(["uv", "build", "--project", ctx.source, "--out-dir", ctx.output / "python-dist"], cwd=ctx.source)


def test(ctx: Context):
    # The installed-worker acceptance test runs against the catalog's Relay host binary.
    os.environ.setdefault("NOMOS_TEST_RELAY_BIN", os.environ["RELAY_BIN"])
    uv(
        ctx,
        "run",
        "--group",
        "test",
        "pytest",
        "-c",
        ctx.source / "pyproject.toml",
        ctx.source / "tests",
        "-q",
    )


def package(ctx: Context):
    ctx.bundle.mkdir(parents=True)
    shutil.copytree(
        ctx.source / PACKAGE, ctx.bundle / PACKAGE, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    for filename in SOURCE_FILES:
        shutil.copy2(ctx.source / filename, ctx.bundle / filename)
    manifest = tomllib.loads((ctx.source / "relay-plugin.toml").read_text(encoding="utf-8"))
    write_runtime_manifest(ctx.bundle, manifest)
    write_attributions(ctx)
    shutil.copy2(ctx.plugin / "THIRD_PARTY_NOTICES.md", ctx.bundle / "THIRD_PARTY_NOTICES.md")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["build", "test", "package"])
    args = parser.parse_args()
    {"build": build, "test": test, "package": package}[args.stage](Context.from_environment())
