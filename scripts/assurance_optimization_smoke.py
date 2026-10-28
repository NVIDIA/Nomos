#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Native Harbor/council wiring proof with a local HTTP fixture, never model inference."""

import argparse
import asyncio
import json
import os
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from evals.nomos.budget import RequestBudget
from evals.nomos.harbor_bridge import HarborExecutor
from evals.nomos.workflow import write
from tests.evals.test_workflow import spec


class FixtureServer(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        response = {"decision": "approve", "reasoning": "Deterministic native wiring control"}
        body = json.dumps(
            {
                "choices": [{"message": {"content": json.dumps(response)}}],
                "usage": {"total_tokens": 0},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


async def smoke(out: Path, python: Path):
    out.mkdir(parents=True, exist_ok=False, mode=0o700)
    config = out / "docker-config"
    config.mkdir()
    plugins = Path("/Applications/Docker.app/Contents/Resources/cli-plugins")
    write(
        config / "config.json", {"cliPluginsExtraDirs": [str(plugins)]} if plugins.exists() else {}
    )
    endpoint = (
        os.environ.get("DOCKER_HOST")
        or subprocess.check_output(
            ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"], text=True
        ).strip()
    )
    os.environ["DOCKER_CONFIG"] = str(config)
    os.environ["DOCKER_HOST"] = endpoint
    image_name = "nomos-evaluation:check-" + uuid.uuid4().hex[:12]
    with (out / "build.log").open("wb") as log:
        subprocess.run(
            ["docker", "build", "-f", "evals/nomos/Dockerfile", "-t", image_name, "."],
            stdout=log,
            stderr=log,
            check=True,
            timeout=300,
        )
    image = subprocess.check_output(
        ["docker", "image", "inspect", image_name, "--format", "{{.Id}}"], text=True
    ).strip()
    os.environ["NOMOS_EVAL_API_KEY"] = "native-wiring-fixture"  # pragma: allowlist secret
    # This server returns fixed test data and never records incoming credentials or prompts.
    server = ThreadingHTTPServer(("0.0.0.0", 0), FixtureServer)  # noqa: S104
    threading.Thread(target=server.serve_forever, daemon=True).start()
    experiment = spec().model_copy(
        update={
            "model": "native-wiring-fixture",
            "versions": {**spec().versions, "runtime_image": image},
            "endpoint": f"http://host.docker.internal:{server.server_port}/v1",
            "trial_seconds": 120,
        }
    )
    budget = RequestBudget.create(out / "budget.json", calls=1, seconds=300)
    executor = HarborExecutor(python=python, image=image, budget=budget)
    try:
        result = await executor(
            experiment.tasks[0].request,
            "candidate",
            experiment,
            "native-wiring-" + uuid.uuid4().hex[:12],
            out / "trial",
            1,
        )
        if result["decision"] != "approve" or result["status"] != "complete":
            raise RuntimeError("native council wiring control failed")
        write(
            out / "receipt.json",
            {
                "subject": "native wiring only; not model performance",
                "result": result,
                "image": image,
                "provider_calls": 0,
            },
        )
        print("Native Harbor council wiring passed; no external model calls")
    finally:
        server.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--harbor-python", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    asyncio.run(smoke(args.out.resolve(), args.harbor_python))


if __name__ == "__main__":
    main()
