# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Repository checks, run by pre-commit (and usable by hand).

    python scripts/checks.py integrity [--fix]   relay-plugin.toml's sha256 matches its artifact
    python scripts/checks.py schema [--fix]      config.schema.json equals settings.SCHEMA
    python scripts/checks.py headers             every source and documentation file carries the SPDX header
    python scripts/checks.py manifest            the manifest follows the catalog's conventions
    python scripts/checks.py lock                uv.lock records the package's own current version
    python scripts/checks.py docs [--fix]        docs/architecture/*.md are rendered from their current templates
    python scripts/checks.py links               relative Markdown links and their anchors resolve

With no names, every check runs. The integrity digest covers only the manifest's `source.artifact` (Relay hashes
that one file).
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "relay-plugin.toml"
SPDX = (
    "SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.",
    "SPDX-License-Identifier: Apache-2.0",
)
HEADER_SUFFIXES = {".py", ".toml", ".md", ".in", ".sh", ".yaml", ".yml", ".cfg"}
NO_HEADER = {"uv.lock", "LICENSE", "NOTICE"}
# Rendered pages carry their template's header; the templates are checked instead.
GENERATED = "docs/architecture/"


def files() -> list[Path]:
    """Tracked and new, not ignored, files; never the vendored submodule."""
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    return [ROOT / f for f in out if not f.startswith("vendor/") and (ROOT / f).is_file()]


def integrity(fix: bool) -> list[str]:
    manifest = tomllib.loads(MANIFEST.read_text())
    artifact = ROOT / manifest["source"]["manifest_root"] / manifest["source"]["artifact"]
    want = "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
    have = manifest["integrity"]["sha256"]
    if have == want:
        return []
    if fix:
        MANIFEST.write_text(MANIFEST.read_text().replace(have, want))
        return [f"updated {MANIFEST.name} integrity to {want}"]
    return [f"{MANIFEST.name}: integrity {have} does not match {artifact.name} ({want}); run with --fix"]


def schema(fix: bool) -> list[str]:
    path = ROOT / "nemo_relay_nomos" / "config.schema.json"
    text = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(ROOT),
            "--frozen",
            "python",
            "-c",
            "from nemo_relay_nomos.settings import schema_text; print(schema_text(), end='')",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    if path.read_text() == text:
        return []
    if fix:
        path.write_text(text)
        return [f"regenerated {path.relative_to(ROOT)}"]
    return [f"{path.relative_to(ROOT)} is stale; run with --fix"]


def headers(_: bool) -> list[str]:
    problems = []
    for path in files():
        rel = path.relative_to(ROOT).as_posix()
        if path.name in NO_HEADER or path.suffix not in HEADER_SUFFIXES or rel.startswith(GENERATED):
            continue
        head = "\n".join(path.read_text().splitlines()[:6])
        if not all(line in head for line in SPDX):
            problems.append(f"{rel}: missing SPDX header")
    return problems


def manifest(_: bool) -> list[str]:
    m = tomllib.loads(MANIFEST.read_text())
    problems = []
    if m.get("defaults", {}).get("enabled") is not False:
        problems.append("manifest: [defaults] enabled must be false")
    if "config_schema" in m and "config_schema" not in m.get("capabilities", {}).get("items", []):
        problems.append("manifest: config_schema needs the config_schema capability")
    entry = m["load"]["entrypoint"].split(":")[0].replace(".", "/") + ".py"
    if entry != m["source"]["artifact"]:
        problems.append(f"manifest: Python worker artifact must be the entrypoint module ({entry})")
    if not re.fullmatch(r">=\d+\.\d+\.\d+,<\d+\.\d+", m["compat"]["relay"]):
        problems.append("manifest: compat.relay must be a bounded range")
    return problems


def lock(_: bool) -> list[str]:
    """The catalog's attribution step refuses a lock whose own-package version differs from pyproject."""
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    packages = tomllib.loads((ROOT / "uv.lock").read_text())["package"]
    locked = next(p["version"] for p in packages if p["name"] == "nemo-relay-nomos")
    if locked == version:
        return []
    return [
        f"uv.lock records nemo-relay-nomos {locked}, pyproject says {version}: uv lock --upgrade-package nemo-relay-nomos"
    ]


def docs(fix: bool) -> list[str]:
    spec = importlib.util.spec_from_file_location("architecture", ROOT / "docs" / "build" / "architecture.py")
    architecture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(architecture)
    stale = [
        name
        for name, text in architecture.render().items()
        if not (architecture.OUT / name).exists() or (architecture.OUT / name).read_text() != text
    ]
    if not stale:
        return []
    if fix:
        architecture.main()
        return [f"re-rendered docs/architecture/{name}" for name in stale]
    return [f"docs/architecture/{name} is stale; run with --fix (or make docs)" for name in stale]


def _slugs(markdown: str) -> set[str]:
    """GitHub's heading anchors: lower case, punctuation dropped, spaces to hyphens, repeats numbered."""
    out, seen = set(), {}
    for heading in re.findall(r"^#{1,6} (.+)$", re.sub(r"```.*?```", "", markdown, flags=re.S), re.M):
        slug = re.sub(r"[^\w\- ]", "", heading.strip().lower().replace("`", "")).replace(" ", "-")
        n = seen.get(slug, 0)
        seen[slug] = n + 1
        out.add(slug if n == 0 else f"{slug}-{n}")
    return out


def links(_: bool) -> list[str]:
    problems = []
    for path in files():
        rel = path.relative_to(ROOT).as_posix()
        # Templates link from where they are rendered (docs/architecture/), so the rendered pages are checked.
        if path.suffix != ".md" or rel.startswith("docs/build/architecture/"):
            continue
        body = re.sub(r"```.*?```", "", path.read_text(), flags=re.S)
        for link in re.findall(r"\]\(([^)\s]+)\)", body):
            if link.startswith(("http://", "https://", "mailto:")):
                continue
            target_path, _, anchor = link.partition("#")
            target = (path.parent / target_path).resolve() if target_path else path
            if not target.exists():
                problems.append(f"{rel}: missing file {link}")
            elif anchor and target.suffix == ".md" and anchor not in _slugs(target.read_text()):
                problems.append(f"{rel}: missing anchor {link}")
    return problems


CHECKS = {
    "integrity": integrity,
    "schema": schema,
    "headers": headers,
    "manifest": manifest,
    "lock": lock,
    "docs": docs,
    "links": links,
}

if __name__ == "__main__":
    names, fix = [a for a in sys.argv[1:] if a != "--fix"] or list(CHECKS), "--fix" in sys.argv
    messages = [msg for name in names for msg in CHECKS[name](fix)]
    for msg in messages:
        print(msg)
    sys.exit(1 if messages else 0)
