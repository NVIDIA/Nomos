#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Run Nomos test suites and render a self-contained HTML report."""

from __future__ import annotations

import argparse
import html
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT = ROOT / "test-reports" / "nomos-test-report.html"
DEFAULT_JUNIT_DIR = ROOT / "test-reports" / "junit"
# Packages measured when --coverage is requested. Mirrors [tool.coverage.run]
# source in pyproject.toml so the Cobertura report SonarQube reads matches the
# local coverage floor.
COVERAGE_SOURCES = ("nomos", "executors", "trusted_executor_contract", "nomos_runtime")
COVERAGE_XML = ROOT / "coverage.xml"


@dataclass(frozen=True)
class SuiteSpec:
    key: str
    label: str
    kind: str
    command: list[str]
    components: tuple[str, ...]
    description: str
    deployed: bool = False
    needs_env: tuple[str, ...] = ()


@dataclass
class SuiteResult:
    spec: SuiteSpec
    status: str
    exit_code: int | None = None
    duration_seconds: float = 0.0
    stdout: str = ""
    stderr: str = ""
    junit_path: Path | None = None
    tests: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0
    reason: str = ""

    @property
    def passed(self) -> int:
        return max(0, self.tests - self.failures - self.errors - self.skipped)


def _pytest_suite(
    key: str,
    label: str,
    paths: Iterable[str],
    components: tuple[str, ...],
    description: str,
    *,
    deployed: bool = False,
    needs_env: tuple[str, ...] = (),
) -> SuiteSpec:
    return SuiteSpec(
        key=key,
        label=label,
        kind="pytest",
        command=["uv", "run", "pytest", *paths, "-q"],
        components=components,
        description=description,
        deployed=deployed,
        needs_env=needs_env,
    )


SUITES: dict[str, SuiteSpec] = {
    "unit": _pytest_suite(
        "unit",
        "Unit and Layer Tests",
        [
            "tests/level_0",
            "tests/level_1",
            "tests/level_2",
            "tests/level_3",
            "tests/level_4",
            "tests/test_agent_accounts_cli.py",
            "tests/test_agent_signup_service.py",
            "tests/test_config.py",
            "tests/test_config_schema.py",
            "tests/test_permissions.py",
            "tests/test_shell.py",
            "tests/test_llm.py",
            "tests/test_council_agent_parsing.py",
            "tests/test_demo_auth.py",
            "tests/test_demo_scenarios.py",
            "tests/test_deploy_stack_script.py",
            "tests/test_run_test_suite.py",
            "tests/test_logging.py",
            "tests/test_key_manager.py",
            "tests/test_key_rotation.py",
            "tests/test_nv_inference_backend.py",
            "tests/test_stateless_worker.py",
            "tests/test_writ_factory.py",
        ],
        (
            "Level 0 primitives",
            "Policy engine",
            "Quorum",
            "Executor primitives",
            "SecurityCouncil",
            "Config",
            "LLM adapters",
        ),
        "Pure and mostly local tests for primitives, schemas, policy, quorum, "
        "LLM parsing, writs, and core council behavior.",
    ),
    "integration": _pytest_suite(
        "integration",
        "Service Integration Tests",
        [
            "tests/test_multi_council_api.py",
            "tests/test_dynamic_councils.py",
            "tests/test_council_registry.py",
            "tests/test_nat_functions.py",
            "tests/test_opa_client.py",
            "tests/test_opa_nat_integration.py",
            "tests/test_opa_policy.py",
            "tests/test_grpc_mcp_client.py",
            "tests/test_grpc_proxy.py",
            "tests/test_mcp_server.py",
            "tests/test_monitor_api.py",
            "tests/test_monitor_proxy.py",
            "tests/test_monitor_metrics.py",
            "tests/test_monitor_ledger.py",
            "tests/test_monitor_source_attribution.py",
            "tests/test_executor_api.py",
            "tests/test_enforcer.py",
            "tests/test_e2e_writ_lifecycle.py",
        ],
        (
            "Dynamic councils",
            "CouncilRegistry",
            "NAT/MCP",
            "OPA",
            "gRPC proxy",
            "Monitor",
            "Executor",
            "Writ lifecycle",
        ),
        "In-process integration tests for routing, dynamic catalog management, "
        "MCP/NAT adapters, OPA, monitor proxying, executor checks, and writ lifecycle.",
    ),
    "redis": _pytest_suite(
        "redis",
        "Redis Integration Tests",
        ["tests/test_authenticator.py", "tests/test_redis_store.py"],
        ("Redis", "Authentication", "Council membership"),
        "Redis-backed account, token, membership, nonce, and rate-limit behavior.",
    ),
    "grpc-live": _pytest_suite(
        "grpc-live",
        "Live gRPC Smoke",
        ["tests/test_grpc_live_smoke.py"],
        ("gRPC proxy", "Monitor", "MCP"),
        "Deployed gRPC proxy smoke tests against the running monitor/MCP service.",
        deployed=True,
        needs_env=("NOMOS_GRPC_SMOKE_ADDR",),
    ),
    "mcp-live": _pytest_suite(
        "mcp-live",
        "Live MCP Multi-Council Read Contract",
        ["tests/test_multi_council_mcp_live.py"],
        ("MCP", "Dynamic councils", "CouncilRegistry"),
        "Deployed MCP tool discovery, catalog read APIs, and v2 routing validation.",
        deployed=True,
        needs_env=("NOMOS_MCP_LIVE_URL",),
    ),
    "mcp-live-council": _pytest_suite(
        "mcp-live-council",
        "Live MCP Council Authorization Contract",
        ["tests/test_mcp_live_integration.py"],
        ("MCP", "Authentication", "Writ lifecycle"),
        "Deployed MCP authorization tests that require a live council URL and agent token.",
        deployed=True,
        needs_env=("NOMOS_MCP_LIVE_URL", "NOMOS_AUTH_TOKEN"),
    ),
    "mcp-live-mutate": _pytest_suite(
        "mcp-live-mutate",
        "Live MCP Dynamic Council Mutation",
        ["tests/test_multi_council_mcp_live.py"],
        ("MCP", "Dynamic councils", "Hot reload"),
        "Opt-in live MCP mutation and hot-reload test for a temporary disabled profile.",
        deployed=True,
        needs_env=("NOMOS_MCP_LIVE_URL", "NOMOS_COUNCIL_MANAGEMENT_TOKEN", "NOMOS_MCP_LIVE_MUTATE"),
    ),
    "smoke-script": SuiteSpec(
        "smoke-script",
        "Deployed Stack Smoke Script",
        "script",
        ["uv", "run", "python", "scripts/test_smoke.py"],
        ("MCP", "Redis", "OPA", "Council", "Executor"),
        "End-to-end deployed smoke script for account setup, council calls, and executor paths.",
        deployed=True,
    ),
}


PROFILE_SUITES = {
    "unit": ("unit",),
    "integration": ("integration", "redis"),
    "smoke": ("grpc-live", "mcp-live", "smoke-script"),
    "live": (
        "grpc-live",
        "mcp-live",
        "mcp-live-council",
        "mcp-live-mutate",
        "smoke-script",
    ),
    "all": (
        "unit",
        "integration",
        "redis",
        "grpc-live",
        "mcp-live",
        "mcp-live-council",
        "smoke-script",
    ),
}


COMPONENTS = (
    "Config",
    "Dynamic councils",
    "CouncilRegistry",
    "NAT/MCP",
    "Authentication",
    "Redis",
    "OPA",
    "SecurityCouncil",
    "LLM adapters",
    "gRPC proxy",
    "Monitor",
    "Executor",
    "Writ lifecycle",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        choices=sorted(PROFILE_SUITES),
        default="all",
        help="Suite profile to run.",
    )
    parser.add_argument(
        "--suite",
        action="append",
        choices=sorted(SUITES),
        help="Run a specific suite. May be supplied more than once; overrides --profile.",
    )
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--junit-dir", type=Path, default=DEFAULT_JUNIT_DIR)
    parser.add_argument(
        "--require-deployed",
        action="store_true",
        help="Fail deployed suites instead of skipping when local services are unavailable.",
    )
    parser.add_argument(
        "--include-mutating-live",
        action="store_true",
        help="Include the live MCP mutation suite in --profile all/smoke when credentials exist.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=900,
        help="Per-suite timeout in seconds.",
    )
    parser.add_argument(
        "--coverage",
        action="store_true",
        help="Collect coverage across pytest suites and write coverage.xml "
        "(Cobertura) for SonarQube.",
    )
    args = parser.parse_args()

    args.report = args.report.resolve()
    args.junit_dir = args.junit_dir.resolve()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.junit_dir.mkdir(parents=True, exist_ok=True)
    _remove_stale_html_reports(args.report)

    env = os.environ.copy()
    _load_deploy_env(env)
    _set_default_live_env(env)
    selected = list(args.suite or PROFILE_SUITES[args.profile])
    if args.include_mutating_live and "mcp-live-mutate" not in selected:
        selected.append("mcp-live-mutate")

    if args.coverage:
        _reset_coverage()

    results: list[SuiteResult] = []
    for key in selected:
        spec = SUITES[key]
        results.append(
            _run_suite(
                spec,
                env=env,
                junit_dir=args.junit_dir,
                timeout=args.timeout,
                require_deployed=args.require_deployed,
                coverage=args.coverage,
            )
        )

    if args.coverage:
        _write_coverage_xml(env)

    _write_report(args.report, results, env)
    print(f"HTML report: {args.report}")

    if any(result.status == "failed" for result in results):
        return 1
    return 0


def _load_deploy_env(env: dict[str, str]) -> None:
    path = ROOT / "deploy" / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        env.setdefault(key, value)
        if key == "COUNCIL_MANAGEMENT_TOKEN":
            env.setdefault("NOMOS_COUNCIL_MANAGEMENT_TOKEN", value)


def _set_default_live_env(env: dict[str, str]) -> None:
    if _http_ok("http://127.0.0.1:9901/healthz"):
        env.setdefault("NOMOS_MCP_LIVE_URL", "http://127.0.0.1:9901/mcp")
    if _tcp_hint_ok("127.0.0.1", 9904):
        env.setdefault("NOMOS_GRPC_SMOKE_ADDR", "127.0.0.1:9904")


def _http_ok(url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            return response.status < 500
    except (OSError, urllib.error.URLError):
        return False


def _tcp_hint_ok(host: str, port: int) -> bool:
    import socket

    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _remove_stale_html_reports(report_path: Path) -> None:
    """Keep the report directory focused on one consolidated HTML report."""
    for candidate in report_path.parent.glob("*.html"):
        if candidate.resolve() != report_path:
            candidate.unlink()


def _pytest_extra_args(junit_path: Path, coverage: bool) -> list[str]:
    """Build the per-suite pytest flags (JUnit always, coverage when requested)."""
    extra = [f"--junitxml={junit_path}"]
    if coverage:
        extra += [f"--cov={pkg}" for pkg in COVERAGE_SOURCES]
        # Accumulate across sequential suites; the Cobertura XML is rendered
        # once after the run. Suppress the per-suite report.
        extra += ["--cov-append", "--cov-report="]
        # Don't let pytest-cov hard-fail on the [tool.coverage.report]
        # fail_under floor: that floor spans the full suite, but this run may
        # be a single profile (e.g. unit-only) that legitimately covers less.
        # Coverage data is still collected and the XML is still written; the
        # floor is enforced separately rather than per profile.
        extra += ["--cov-fail-under=0"]
    return extra


def _reset_coverage() -> None:
    """Drop stale coverage data/report so a fresh --cov-append run starts clean."""
    for path in [ROOT / ".coverage", COVERAGE_XML, *ROOT.glob(".coverage.*")]:
        path.unlink(missing_ok=True)


def _write_coverage_xml(env: dict[str, str]) -> None:
    """Render accumulated coverage data to Cobertura XML for SonarQube.

    `coverage xml` exits non-zero when total coverage is under the
    [tool.coverage.report] fail_under floor, but it still writes the report
    first. That floor is enforced separately; here we only need the XML to
    exist for the scanner, so success is judged by the file, not the exit code.
    """
    result = subprocess.run(
        ["uv", "run", "coverage", "xml", "-o", str(COVERAGE_XML)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if COVERAGE_XML.exists():
        print(f"Coverage XML: {COVERAGE_XML}")
    else:
        print(
            f"WARNING: coverage xml did not produce {COVERAGE_XML.name}:\n"
            f"{result.stdout}{result.stderr}",
            file=sys.stderr,
        )


def _run_suite(
    spec: SuiteSpec,
    *,
    env: dict[str, str],
    junit_dir: Path,
    timeout: int,
    require_deployed: bool,
    coverage: bool = False,
) -> SuiteResult:
    suite_env = env.copy()
    if spec.key == "mcp-live-mutate":
        suite_env.setdefault("NOMOS_MCP_LIVE_MUTATE", "1")

    missing_env = [key for key in spec.needs_env if not suite_env.get(key)]
    if spec.deployed and (missing_env or not _http_ok("http://127.0.0.1:9901/healthz")):
        reason = (
            "missing " + ", ".join(missing_env) if missing_env else "deployed stack unavailable"
        )
        if not require_deployed:
            return SuiteResult(spec=spec, status="skipped", reason=reason)

    command = list(spec.command)
    junit_path: Path | None = None
    if spec.kind == "pytest":
        junit_path = junit_dir / f"{spec.key}.xml"
        command += _pytest_extra_args(junit_path, coverage)

    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=suite_env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        duration = time.monotonic() - started
    except subprocess.TimeoutExpired as exc:
        return SuiteResult(
            spec=spec,
            status="failed",
            exit_code=None,
            duration_seconds=time.monotonic() - started,
            stdout=_subprocess_output_text(exc.stdout),
            stderr=_subprocess_output_text(exc.stderr) or f"Timed out after {timeout}s",
            junit_path=junit_path,
            reason="timeout",
        )

    result = SuiteResult(
        spec=spec,
        status="passed" if completed.returncode == 0 else "failed",
        exit_code=completed.returncode,
        duration_seconds=duration,
        stdout=completed.stdout,
        stderr=completed.stderr,
        junit_path=junit_path,
    )
    if junit_path and junit_path.exists():
        _read_junit_counts(junit_path, result)
    return result


def _subprocess_output_text(output: str | bytes | None) -> str:
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    return output or ""


def _read_junit_counts(path: Path, result: SuiteResult) -> None:
    root = ET.parse(path).getroot()  # noqa: S314 - pytest generated this local JUnit XML.
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    result.tests = sum(int(suite.attrib.get("tests", 0)) for suite in suites)
    result.failures = sum(int(suite.attrib.get("failures", 0)) for suite in suites)
    result.errors = sum(int(suite.attrib.get("errors", 0)) for suite in suites)
    result.skipped = sum(int(suite.attrib.get("skipped", 0)) for suite in suites)


def _write_report(path: Path, results: list[SuiteResult], env: dict[str, str]) -> None:
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    duration_seconds = sum(r.duration_seconds for r in results)
    duration_label = f"{duration_seconds:.1f}s"
    mutate_setting = env.get("NOMOS_MCP_LIVE_MUTATE", "")
    if not mutate_setting and any(
        result.spec.key == "mcp-live-mutate" and result.status != "skipped" for result in results
    ):
        mutate_setting = "set per mutating suite"
    totals = {
        "tests": sum(r.tests for r in results),
        "passed": sum(r.passed for r in results),
        "failures": sum(r.failures for r in results),
        "errors": sum(r.errors for r in results),
        "skipped": sum(r.skipped for r in results)
        + sum(1 for r in results if r.status == "skipped" and r.tests == 0),
        "duration": duration_seconds,
    }
    overall = "failed" if any(r.status == "failed" for r in results) else "passed"
    if all(r.status == "skipped" for r in results):
        overall = "skipped"

    html_text = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Nomos Test Report</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f5f7fb;
      --panel: #ffffff;
      --ink: #172033;
      --muted: #617089;
      --line: #d9e1ef;
      --ok: #0f8a5f;
      --bad: #c93c37;
      --skip: #8b6f16;
      --blue: #2457d6;
      --cyan: #0d7f96;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font: 14px/1.5 Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
        "Segoe UI", sans-serif;
      color: var(--ink);
      background: var(--bg);
    }}
    header {{
      padding: 32px max(24px, 7vw);
      background: linear-gradient(135deg, #111b33 0%, #1f5f74 100%);
      color: white;
    }}
    header h1 {{ margin: 0 0 8px; font-size: 34px; letter-spacing: 0; }}
    header p {{ margin: 0; color: #d9e8f1; max-width: 780px; }}
    main {{ padding: 28px max(24px, 7vw) 48px; }}
    section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 20px;
      margin: 0 0 20px;
      box-shadow: 0 10px 24px rgba(20, 36, 70, 0.06);
    }}
    h2 {{ margin: 0 0 14px; font-size: 20px; }}
    h3 {{ margin: 18px 0 8px; font-size: 15px; }}
    .summary {{
      display: grid;
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 12px;
    }}
    .metric {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      background: #fbfcff;
    }}
    .metric strong {{ display: block; font-size: 24px; }}
    .metric span {{ color: var(--muted); }}
    .status {{
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 3px 10px;
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: .04em;
    }}
    .status.passed {{ color: var(--ok); background: #e7f6ef; }}
    .status.failed {{ color: var(--bad); background: #fdecea; }}
    .status.skipped {{ color: var(--skip); background: #fbf3d5; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 10px 8px; text-align: left; }}
    th {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }}
    code, pre {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
    pre {{
      white-space: pre-wrap;
      max-height: 260px;
      overflow: auto;
      background: #101828;
      color: #e8eef8;
      padding: 12px;
      border-radius: 8px;
    }}
    .grid2 {{ display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(0, .9fr); gap: 18px; }}
    .diagram {{ width: 100%; height: auto; border: 1px solid var(--line); border-radius: 8px; }}
    .matrix td:not(:first-child), .matrix th:not(:first-child) {{ text-align: center; }}
    .dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; background: var(--blue); }}
    .muted {{ color: var(--muted); }}
    @media (max-width: 900px) {{
      .summary, .grid2 {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Nomos Test Report</h1>
    <p>Generated {html.escape(generated)}. Overall status:
      <span class="status {overall}">{overall}</span>
    </p>
  </header>
  <main>
    <section>
      <h2>Run Summary</h2>
      <div class="summary">
        {_metric("Suites", str(len(results)))}
        {_metric("Tests", str(totals["tests"]))}
        {_metric("Passed", str(totals["passed"]))}
        {_metric("Failures", str(totals["failures"] + totals["errors"]))}
        {_metric("Skipped", str(totals["skipped"]))}
        {_metric("Duration", duration_label)}
      </div>
    </section>
    <section class="grid2">
      <div>
        <h2>Component Flow</h2>
        {_architecture_svg()}
      </div>
      <div>
        <h2>What This Run Covered</h2>
        <p class="muted">
          Unit suites exercise local contracts. Integration suites bind adapters and service
          boundaries. Smoke suites verify deployed interfaces when reachable.
        </p>
        {_coverage_list(results)}
      </div>
    </section>
    <section>
      <h2>Suite Results</h2>
      <table>
        <thead>
          <tr>
            <th>Suite</th><th>Status</th><th>Tests</th><th>Passed</th>
            <th>Failed</th><th>Skipped</th><th>Duration</th><th>Command</th>
          </tr>
        </thead>
        <tbody>
          {"".join(_suite_row(result) for result in results)}
        </tbody>
      </table>
    </section>
    <section>
      <h2>Component Matrix</h2>
      {_component_matrix(results)}
    </section>
    <section>
      <h2>Environment</h2>
      <table>
        <tbody>
          {_env_row("NOMOS_MCP_LIVE_URL", env.get("NOMOS_MCP_LIVE_URL", ""))}
          {_env_row("NOMOS_GRPC_SMOKE_ADDR", env.get("NOMOS_GRPC_SMOKE_ADDR", ""))}
          {_env_row("NOMOS_MCP_LIVE_MUTATE", mutate_setting)}
          {_env_row("COUNCIL_MANAGEMENT_TOKEN", "set" if env.get("COUNCIL_MANAGEMENT_TOKEN") else "")}
        </tbody>
      </table>
    </section>
    <section>
      <h2>Captured Output</h2>
      {"".join(_output_block(result) for result in results)}
    </section>
  </main>
</body>
</html>
"""
    path.write_text(html_text, encoding="utf-8")


def _metric(label: str, value: str) -> str:
    return f'<div class="metric"><strong>{html.escape(value)}</strong><span>{html.escape(label)}</span></div>'


def _suite_row(result: SuiteResult) -> str:
    failed = result.failures + result.errors
    command = " ".join(shlex.quote(part) for part in result.spec.command)
    if result.status == "skipped" and result.reason:
        command = f"{command} ({result.reason})"
    return (
        "<tr>"
        f'<td><strong>{html.escape(result.spec.label)}</strong><br><span class="muted">'
        f"{html.escape(result.spec.description)}</span></td>"
        f'<td><span class="status {result.status}">{result.status}</span></td>'
        f"<td>{result.tests}</td><td>{result.passed}</td><td>{failed}</td>"
        f"<td>{result.skipped}</td><td>{result.duration_seconds:.1f}s</td>"
        f"<td><code>{html.escape(command)}</code></td>"
        "</tr>"
    )


def _env_row(key: str, value: str) -> str:
    display = value or "<not set>"
    return f"<tr><th>{html.escape(key)}</th><td>{html.escape(display)}</td></tr>"


def _output_block(result: SuiteResult) -> str:
    text = (result.stdout + "\n" + result.stderr).strip()
    if not text:
        text = result.reason or "No output captured."
    return f"<h3>{html.escape(result.spec.label)}</h3><pre>{html.escape(text[-6000:])}</pre>"


def _coverage_list(results: list[SuiteResult]) -> str:
    covered: set[str] = set()
    for result in results:
        if result.status == "passed":
            covered.update(result.spec.components)
    items = "".join(f"<li>{html.escape(component)}</li>" for component in sorted(covered))
    return f"<ul>{items}</ul>"


def _component_matrix(results: list[SuiteResult]) -> str:
    header = "".join(f"<th>{html.escape(result.spec.key)}</th>" for result in results)
    rows = []
    for component in COMPONENTS:
        cells = []
        for result in results:
            hit = component in result.spec.components
            marker = '<span class="dot" title="covered"></span>' if hit else ""
            cells.append(f"<td>{marker}</td>")
        rows.append(f"<tr><td>{html.escape(component)}</td>{''.join(cells)}</tr>")
    return (
        '<table class="matrix"><thead><tr><th>Component</th>'
        f"{header}</tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def _architecture_svg() -> str:
    boxes = [
        ("Client", 20, 60, "#2457d6"),
        ("Monitor / MCP", 170, 60, "#0d7f96"),
        ("CouncilRegistry", 340, 60, "#6b57d6"),
        ("Council Profiles", 520, 60, "#9a5b16"),
        ("OPA", 170, 170, "#4a7c2b"),
        ("SecurityCouncil", 340, 170, "#ba3f55"),
        ("Writ Executor", 520, 170, "#44546a"),
    ]
    rects = []
    for label, x, y, color in boxes:
        rects.append(
            f'<rect x="{x}" y="{y}" width="130" height="48" rx="8" fill="{color}" />'
            f'<text x="{x + 65}" y="{y + 30}" text-anchor="middle" '
            f'font-size="13" fill="white">{html.escape(label)}</text>'
        )
    arrows = [
        (150, 84, 170, 84),
        (300, 84, 340, 84),
        (470, 84, 520, 84),
        (235, 108, 235, 170),
        (405, 108, 405, 170),
        (470, 194, 520, 194),
    ]
    lines = [
        f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#172033" '
        'stroke-width="2" marker-end="url(#arrow)" />'
        for x1, y1, x2, y2 in arrows
    ]
    return (
        '<svg class="diagram" viewBox="0 0 680 260" role="img" '
        'aria-label="Nomos tested component flow">'
        '<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" '
        'refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 Z" fill="#172033" />'
        "</marker></defs>"
        '<rect x="1" y="1" width="678" height="258" rx="10" fill="#f8fbff" />'
        f"{''.join(lines)}{''.join(rects)}"
        '<text x="20" y="235" font-size="12" fill="#617089">'
        "Report maps suite results onto the request path, catalog management path, and execution path."
        "</text></svg>"
    )


if __name__ == "__main__":
    sys.exit(main())
