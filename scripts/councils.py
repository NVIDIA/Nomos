#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


# ruff: noqa: E402
"""Manage dynamic Nomos council profiles and catalog entries."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from nomos.config.council_catalog_store import (
    TEMPLATE_CONFIGS,
    CouncilCatalogStore,
    CouncilCatalogStoreError,
)


def print_result(result: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return

    status = result.get("status")
    if status:
        print(f"Status:      {status}")
    if result.get("message"):
        print(f"Message:     {result['message']}")
    if result.get("council_id"):
        print(f"Council ID:  {result['council_id']}")
    if "enabled" in result:
        print(f"Enabled:     {result['enabled']}")
    if result.get("opa_policy_id"):
        print(f"OPA Policy:  {result['opa_policy_id']}")
    if result.get("ledger_path"):
        print(f"Ledger:      {result['ledger_path']}")
    if result.get("members") and isinstance(result["members"], list):
        members = result["members"]
        if members and isinstance(members[0], str):
            print(f"Members:     {', '.join(members)}")
    if result.get("councils"):
        for council in result["councils"]:
            default = " default" if council.get("default") else ""
            enabled = "enabled" if council.get("enabled") else "disabled"
            print(f"{council['council_id']} ({enabled}{default})")


def store_from_args(args: argparse.Namespace) -> CouncilCatalogStore:
    if args.catalog:
        if args.profile_dir:
            return CouncilCatalogStore(catalog_path=args.catalog, profile_dir=args.profile_dir)
        return CouncilCatalogStore(
            catalog_path=args.catalog,
            profile_dir=args.catalog.parent / "councils",
        )
    store = CouncilCatalogStore.from_env()
    if args.profile_dir:
        return CouncilCatalogStore(catalog_path=store.catalog_path, profile_dir=args.profile_dir)
    return store


def cmd_list(args: argparse.Namespace) -> dict[str, Any]:
    store = store_from_args(args)
    catalog = store.validate()
    return {
        "status": "ok",
        "default_council_id": catalog.default_council_id,
        "councils": store.list_councils(),
    }


def cmd_show(args: argparse.Namespace) -> dict[str, Any]:
    store = store_from_args(args)
    return {"status": "ok", **store.show_council(args.council_id)}


def cmd_validate(args: argparse.Namespace) -> dict[str, Any]:
    store = store_from_args(args)
    catalog = store.validate()
    return {
        "status": "ok",
        "message": "catalog validates",
        "default_council_id": catalog.default_council_id,
        "councils": list(store.list_councils()),
    }


def cmd_create(args: argparse.Namespace) -> dict[str, Any]:
    store = store_from_args(args)
    settings = store.create_council(
        args.council_id,
        template=args.template,
        description=args.description,
        use_case=args.use_case,
        enabled=args.enabled,
        force=args.force,
    )
    return {
        "status": "created",
        "message": "council profile created; running services reload on next request",
        "council_id": settings.council_id,
        "enabled": settings.enabled,
        "opa_policy_id": settings.opa.policy_id,
        "ledger_path": settings.ledger_path,
        "members": [member.id for member in settings.members],
    }


def cmd_update_from_file(args: argparse.Namespace) -> dict[str, Any]:
    store = store_from_args(args)
    enabled = None
    if args.enabled:
        enabled = True
    if args.disabled:
        enabled = False
    settings = store.update_council_from_file(
        args.council_id,
        args.profile_yaml,
        enabled=enabled,
    )
    return {
        "status": "updated",
        "message": "council profile updated; running services reload on next request",
        "council_id": settings.council_id,
        "enabled": settings.enabled,
        "opa_policy_id": settings.opa.policy_id,
        "ledger_path": settings.ledger_path,
        "members": [member.id for member in settings.members],
    }


def cmd_set_enabled(args: argparse.Namespace, enabled: bool) -> dict[str, Any]:
    store = store_from_args(args)
    settings = store.set_enabled(args.council_id, enabled)
    return {
        "status": "enabled" if enabled else "disabled",
        "message": "running services reload on next request",
        "council_id": settings.council_id,
        "enabled": settings.enabled,
        "opa_policy_id": settings.opa.policy_id,
        "ledger_path": settings.ledger_path,
        "members": [member.id for member in settings.members],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage dynamic Nomos councils")
    parser.add_argument(
        "--catalog",
        type=Path,
        help="Catalog YAML path. Defaults to COUNCIL_CATALOG_PATH or configs/local_council_catalog.yaml",
    )
    parser.add_argument("--profile-dir", type=Path, help="Directory for generated profiles")
    parser.add_argument("--json", action="store_true", help="Print JSON output")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List configured councils")
    list_parser.set_defaults(handler=cmd_list)

    show = subparsers.add_parser("show", help="Show one council")
    show.add_argument("council_id")
    show.set_defaults(handler=cmd_show)

    validate = subparsers.add_parser("validate", help="Validate the catalog")
    validate.set_defaults(handler=cmd_validate)

    create = subparsers.add_parser("create", help="Create a new council profile")
    create.add_argument("council_id")
    create.add_argument(
        "--template",
        default="strict-production",
        choices=sorted(TEMPLATE_CONFIGS),
        help="Profile template to copy",
    )
    create.add_argument("--description", default="", help="Short operator description")
    create.add_argument("--use-case", default="", help="Use-case notes stored as metadata")
    create.add_argument("--enabled", action="store_true", help="Enable immediately")
    create.add_argument("--force", action="store_true", help="Replace an existing catalog entry")
    create.set_defaults(handler=cmd_create)

    update = subparsers.add_parser(
        "update-from-file",
        help="Replace or add a council profile from a YAML file",
    )
    update.add_argument("council_id")
    update.add_argument("profile_yaml", type=Path)
    update_state = update.add_mutually_exclusive_group()
    update_state.add_argument("--enabled", action="store_true", help="Enable after update")
    update_state.add_argument("--disabled", action="store_true", help="Disable after update")
    update.set_defaults(handler=cmd_update_from_file)

    enable = subparsers.add_parser("enable", help="Enable a file-backed council")
    enable.add_argument("council_id")
    enable.set_defaults(handler=lambda args: cmd_set_enabled(args, True))

    disable = subparsers.add_parser("disable", help="Disable a file-backed council")
    disable.add_argument("council_id")
    disable.set_defaults(handler=lambda args: cmd_set_enabled(args, False))
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = args.handler(args)
    except CouncilCatalogStoreError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print_result(result, as_json=args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
