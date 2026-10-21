#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Build and deploy the isolated synthetic PoC to an existing local Kind cluster."""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import subprocess
from pathlib import Path

from nomos.level_0.crypto import KeyPair

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".assurance-poc"
NAMESPACE = "nomos-assurance-poc"
PINS = {
    "compass": (
        "nemo-compass",
        "518ad41d583e259205fb89623cb035ae3904f3f8",  # pragma: allowlist secret
    ),  # pragma: allowlist secret
    "eval-author": (
        "eval-author",
        "20f95a0a010d4905c13065678af51970404910c9",  # pragma: allowlist secret
    ),  # pragma: allowlist secret
}


def run(args, **kwargs):
    return subprocess.run(args, cwd=ROOT, text=True, check=True, **kwargs)


def kube(context, args, **kwargs):
    return run(["kubectl", "--context", context, "-n", NAMESPACE, *args], **kwargs)


def private_write(path, text):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(text)
    path.chmod(0o600)


def bootstrap(*, with_export=False):
    STATE.mkdir(exist_ok=True, mode=0o700)
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    paths = {}
    for name, (repo, pin) in PINS.items():
        if name == "eval-author" and not with_export:
            continue
        override = os.environ.get("COMPASS_ROOT" if name == "compass" else "EVAL_AUTHOR_ROOT")
        path = Path(override).resolve() if override else STATE / "upstream" / name
        if not path.exists():
            path.mkdir(parents=True)
            run(["git", "init", "--quiet", str(path)], env=env)
            run(
                [
                    "git",
                    "-C",
                    str(path),
                    "fetch",
                    "--depth",
                    "1",
                    f"https://github.com/NVIDIA-NeMo/{repo}.git",
                    pin,
                ],
                env=env,
            )
            run(["git", "-C", str(path), "checkout", "--detach", "FETCH_HEAD"], env=env)
        actual = run(
            ["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True
        ).stdout.strip()
        dirty = run(
            ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
        ).stdout
        if actual != pin or dirty:
            raise RuntimeError(f"{name} must be a clean checkout of {pin}")
        paths[name] = path
    return paths


def credentials():
    path = STATE / "access.env"
    if path.exists():
        path.chmod(0o600)
        values = dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)
        # Never silently generate a new signer against an existing persistent chain.
        if not (STATE / "signing.key").exists() or not (STATE / "trusted-keys.json").exists():
            raise RuntimeError("existing deployment is missing its signing material; restore it")
        if not values.get("API_READ_TOKEN"):
            values["API_READ_TOKEN"] = secrets.token_urlsafe(32)
            private_write(path, "\n".join(f"{k}={v}" for k, v in values.items()) + "\n")
        return values
    values = {
        "DASHBOARD_USER": "reviewer",
        "DASHBOARD_PASSWORD": secrets.token_urlsafe(24),
        "API_READ_TOKEN": secrets.token_urlsafe(32),
        "SOURCE_TOKEN": secrets.token_urlsafe(32),
        "FIXTURE_TOKEN": secrets.token_urlsafe(32),
        "POSTGRES_PASSWORD": secrets.token_urlsafe(24),
    }
    key = KeyPair.generate("assurance-demo-signer")
    descriptor = os.open(STATE / "signing.key", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(key.private_key)
    private_write(STATE / "trusted-keys.json", json.dumps({key.key_id: key.public_key_hex()}))
    private_write(path, "\n".join(f"{k}={v}" for k, v in values.items()) + "\n")
    return values


def apply_object(context, obj):
    # Secrets go only through stdin; never in process arguments or command output.
    kube(context, ["apply", "-f", "-"], input=json.dumps(obj), capture_output=True)


def deploy(cluster, *, with_export=False):
    context = f"kind-{cluster}"
    clusters = run(["kind", "get", "clusters"], capture_output=True).stdout.split()
    if cluster not in clusters:
        raise RuntimeError(f"Kind cluster {cluster!r} is absent; create it explicitly first")
    paths = bootstrap(with_export=with_export)
    values = credentials()
    build = [
        "docker",
        "build",
        "-f",
        "deploy/assurance/Dockerfile",
        "-t",
        "nomos-assurance:poc",
    ]
    if with_export:
        build.extend(
            [
                "--target",
                "assurance-export",
                "--build-context",
                f"evalauthor={paths['eval-author']}",
            ]
        )
    run([*build, "."])
    run(["kind", "load", "docker-image", "nomos-assurance:poc", "--name", cluster])
    apply_object(
        context,
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": NAMESPACE, "labels": {"app.kubernetes.io/part-of": NAMESPACE}},
        },
    )
    groups = {
        "source-access": {"SOURCE_TOKEN": values["SOURCE_TOKEN"]},
        "fixture-access": {"FIXTURE_TOKEN": values["FIXTURE_TOKEN"]},
        "dashboard-access": {
            k: values[k] for k in ("DASHBOARD_USER", "DASHBOARD_PASSWORD", "API_READ_TOKEN")
        },
        "database": {
            "POSTGRES_USER": "nomos",
            "POSTGRES_DB": "nomos",
            "POSTGRES_PASSWORD": values["POSTGRES_PASSWORD"],
        },
        "database-connection": {
            "DATABASE_URL": f"postgresql://nomos:{values['POSTGRES_PASSWORD']}@ledger-db:5432/nomos"
        },
    }
    for name, data in groups.items():
        apply_object(
            context,
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": name, "namespace": NAMESPACE},
                "stringData": data,
            },
        )
    apply_object(
        context,
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "ledger-signing-key", "namespace": NAMESPACE},
            "data": {
                "signing.key": base64.b64encode((STATE / "signing.key").read_bytes()).decode()
            },
        },
    )
    apply_object(
        context,
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "ledger-trust", "namespace": NAMESPACE},
            "data": {"trusted-keys.json": (STATE / "trusted-keys.json").read_text()},
        },
    )
    kube(context, ["apply", "-f", "deploy/assurance/kind.yaml"])
    kube(
        context,
        [
            "set",
            "env",
            "deployment/observer",
            "ASSURANCE_ENABLED=true",
            f"ASSURANCE_EXPORT_ENABLED={str(with_export).lower()}",
        ],
    )
    kube(context, ["rollout", "status", "statefulset/ledger-db", "--timeout=180s"])
    # Same image tag is intentional for a local PoC; force existing pods to pick up rebuilds.
    kube(context, ["rollout", "restart", "deployment/ledger-source", "deployment/observer"])
    for name in ("ledger-source", "observer"):
        kube(context, ["rollout", "status", f"deployment/{name}", "--timeout=180s"])
    print(f"Deployed in {context}/{NAMESPACE}. Credentials: {STATE / 'access.env'}")
    print(
        f"Dashboard: kubectl --context {context} -n {NAMESPACE} port-forward --address 127.0.0.1 svc/observer 9910:8080"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["bootstrap", "deploy"])
    parser.add_argument("--cluster", default="kind")
    parser.add_argument(
        "--with-export",
        action="store_true",
        help="Include pinned Eval Author validation tooling and enable exports",
    )
    args = parser.parse_args()
    if args.action == "bootstrap":
        bootstrap(with_export=args.with_export)
    else:
        deploy(args.cluster, with_export=args.with_export)


if __name__ == "__main__":
    main()
