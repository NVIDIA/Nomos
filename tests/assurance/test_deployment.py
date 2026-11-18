# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Render deployment contracts without requiring a running Docker daemon/cluster."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy/kubernetes/nomos"
EXAMPLE = ROOT / "deploy/assurance/helm-values.yaml"
ETHOS = ROOT / "deploy/assurance/helm-ethos-values.yaml"


def missing_tool(message: str) -> None:
    if os.environ.get("CI"):
        pytest.fail(message)
    pytest.skip(message)


def helm(*args: str) -> subprocess.CompletedProcess[str]:
    binary = shutil.which("helm")
    if not binary:
        missing_tool("Helm is not installed")
    return subprocess.run(
        [binary, "template", "nomos", str(CHART), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )


def objects(result: subprocess.CompletedProcess[str]) -> list[dict]:
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def component(docs: list[dict], kind: str, suffix: str) -> dict:
    return next(d for d in docs if d["kind"] == kind and d["metadata"]["name"] == suffix)


def environment(deployment: dict) -> dict:
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e for e in container["env"]}


def test_helm_disabled_needs_no_assurance_configuration() -> None:
    docs = objects(helm())
    assert all("assurance" not in d["metadata"]["name"] for d in docs)


def test_helm_deterministic_deployment_persists_state_and_isolates_credentials() -> None:
    docs = objects(helm("-f", str(EXAMPLE)))
    observer = component(docs, "Deployment", "nomos-assurance")
    bridge = component(docs, "Deployment", "nomos-assurance-reader")
    assert observer["spec"]["replicas"] == 1
    assert observer["spec"]["strategy"] == {"type": "Recreate"}
    env = environment(observer)
    assert env["ASSURANCE_ENABLED"]["value"] == "true"
    assert env["ASSURANCE_ETHOS_ENABLED"]["value"] == "false"
    assert "ASSURANCE_API_KEY" not in env
    assert env["API_READ_TOKEN"]["valueFrom"]["secretKeyRef"]["key"] == "apiReadToken"
    assert not any("REDIS" in name or "DATABASE" in name for name in env)
    bridge_env = environment(bridge)
    assert bridge_env["COUNCIL_SOURCE_URL"]["value"] == "http://nomos-council:9901"
    assert "DASHBOARD_PASSWORD" not in bridge_env
    assert "ASSURANCE_API_KEY" not in bridge_env
    volumes = observer["spec"]["template"]["spec"]["volumes"]
    assert any(v.get("persistentVolumeClaim") for v in volumes)
    component(docs, "PersistentVolumeClaim", "nomos-assurance-state")
    for name in ("nomos-assurance", "nomos-assurance-reader"):
        assert component(docs, "Service", name)["spec"]["type"] == "ClusterIP"
        container = component(docs, "Deployment", name)["spec"]["template"]["spec"]["containers"][0]
        assert "--no-access-log" in container["args"]
    assert all(d["kind"] != "Ingress" for d in docs)


def test_helm_ethos_mounts_reviewed_documents_and_approved_egress() -> None:
    docs = objects(helm("-f", str(EXAMPLE), "-f", str(ETHOS)))
    observer = component(docs, "Deployment", "nomos-assurance")
    env = environment(observer)
    assert env["ASSURANCE_ETHOS_ENABLED"]["value"] == "true"
    assert env["ASSURANCE_ETHOS_ANALYSIS_SECONDS"]["value"] == "86400"
    assert env["ASSURANCE_ETHOS_WINDOW_SIZE"]["value"] == "10000"
    assert env["ASSURANCE_API_KEY"]["valueFrom"]["secretKeyRef"]["key"] == "inferenceApiKey"
    mounts = observer["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    assert next(v for v in mounts if v["name"] == "ethos")["readOnly"] is True
    policy = component(docs, "NetworkPolicy", "nomos-assurance")["spec"]
    assert policy["ingress"] == []
    assert policy["egress"][-1]["to"][0]["podSelector"]["matchLabels"]["app"] == (
        "approved-inference-gateway"
    )
    reader_policy = component(docs, "NetworkPolicy", "nomos-assurance-reader")["spec"]
    assert (
        reader_policy["egress"][-1]["to"][0]["podSelector"]["matchLabels"][
            "app.kubernetes.io/component"
        ]
        == "council"
    )
    council = component(docs, "NetworkPolicy", "nomos-council")["spec"]
    peers = council["ingress"][0]["from"]
    assert any(
        p["podSelector"]["matchLabels"].get("app.kubernetes.io/component") == "assurance-reader"
        for p in peers
    )


@pytest.mark.parametrize(
    ("args", "error"),
    [
        (["--set", "assurance.enabled=true"], "assurance.councilId is required"),
        (["--set", "assurance.ethos.enabled=true"], "requires assurance.enabled"),
        (
            ["-f", str(EXAMPLE), "--set", "assurance.existingSecret="],
            "assurance.existingSecret is required",
        ),
        (
            ["-f", str(EXAMPLE), "--set", "assurance.ethos.enabled=true"],
            "assurance.ethos.configMap is required",
        ),
        (
            ["-f", str(EXAMPLE), "-f", str(ETHOS), "--set-json", "assurance.inferenceEgress=[]"],
            "requires assurance.inferenceEgress",
        ),
    ],
)
def test_helm_missing_or_inconsistent_settings_fail(args: list[str], error: str) -> None:
    result = helm(*args)
    assert result.returncode != 0
    assert error in result.stderr


def test_helm_existing_claim_is_not_recreated() -> None:
    docs = objects(
        helm("-f", str(EXAMPLE), "--set", "assurance.persistence.existingClaim=retained-state")
    )
    assert all(d["metadata"]["name"] != "nomos-assurance-state" for d in docs)
    observer = component(docs, "Deployment", "nomos-assurance")
    volumes = observer["spec"]["template"]["spec"]["volumes"]
    assert next(v for v in volumes if v["name"] == "state")["persistentVolumeClaim"] == {
        "claimName": "retained-state"
    }


@pytest.mark.parametrize(
    ("enabled", "limits"),
    [
        (False, {}),
        (True, {}),
        (True, {"ASSURANCE_STREAM_TIMEOUT": "240", "ASSURANCE_MAX_MODEL_CALLS": "40"}),
    ],
)
def test_compose_profile_is_opt_in_and_does_not_need_secrets_to_parse(
    enabled: bool, limits: dict[str, str]
) -> None:
    binary = shutil.which("docker")
    if not binary:
        missing_tool("Docker Compose is not installed")
    if subprocess.run([binary, "compose", "version"], capture_output=True).returncode:
        missing_tool("Docker Compose is not installed")
    # Do not consume developer credentials or .env content during config validation.
    env = {k: v for k, v in os.environ.items() if not k.startswith("ASSURANCE_")}
    env.update(limits)
    args = [
        binary,
        "compose",
        "--env-file",
        os.devnull,
        "-f",
        str(ROOT / "deploy/docker-compose.yml"),
        "-f",
        str(ROOT / "deploy/docker-compose.assurance.yml"),
    ]
    if enabled:
        args.extend(["--profile", "assurance"])
    result = subprocess.run(
        [*args, "config", "--format", "json"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    services = yaml.safe_load(result.stdout)["services"]
    assert ("assurance" in services) is enabled
    assert ("assurance-reader" in services) is enabled
    if enabled:
        for name in ("assurance", "assurance-reader"):
            assert "--no-access-log" in services[name]["command"]
        observer = services["assurance"]
        assert observer["ports"][0]["host_ip"] == "127.0.0.1"
        assert not services["assurance-reader"].get("ports")
        assert observer["environment"]["ASSURANCE_ETHOS_ENABLED"] == "false"
        assert observer["environment"]["ASSURANCE_ENABLED"] == "true"
        assert observer["environment"]["ASSURANCE_STREAM_TIMEOUT"] == limits.get(
            "ASSURANCE_STREAM_TIMEOUT", "120"
        )
        assert observer["environment"]["ASSURANCE_MAX_MODEL_CALLS"] == limits.get(
            "ASSURANCE_MAX_MODEL_CALLS", "20"
        )
        assert "nomos-net" not in observer["networks"]
        assert "nomos-net" in services["assurance-reader"]["networks"]
        mount = next(m for m in observer["volumes"] if m["type"] == "bind")
        assert mount["read_only"] is True
        assert mount.get("bind", {}).get("create_host_path", False) is False


def test_compose_tmpfs_mounts_have_absolute_targets():
    config = yaml.safe_load((ROOT / "deploy/docker-compose.assurance.yml").read_text())
    for service in config["services"].values():
        assert all(mount.startswith("/") for mount in service.get("tmpfs", []))


@pytest.mark.parametrize("ci", [True, False])
def test_missing_deployment_tools_fail_in_ci_and_skip_locally(monkeypatch, ci):
    if ci:
        monkeypatch.setenv("CI", "true")
    else:
        monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(shutil, "which", lambda _: None)
    error = pytest.fail.Exception if ci else pytest.skip.Exception
    with pytest.raises(error, match="Helm"):
        helm()
    with pytest.raises(error, match="Docker Compose"):
        test_compose_profile_is_opt_in_and_does_not_need_secrets_to_parse(False, {})
