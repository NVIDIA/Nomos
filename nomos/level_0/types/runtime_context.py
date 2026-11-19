# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Bounded host-reported runtime facts, separate from arguments and approval."""

from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from nomos.strict_json import DuplicateSafeJsonModel

MAX_RUNTIME_CONTEXT_BYTES = 16_384


def _absolute_path(value: str) -> str:
    """Validate a remote path lexically; never inspect this service's filesystem."""
    if value != value.strip() or re.search(r"[\x00-\x1f\x7f]", value):
        raise ValueError("runtime paths must not contain surrounding whitespace or controls")
    if not (PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()):
        raise ValueError("runtime paths must be absolute")
    if any(part in {".", ".."} for part in re.split(r"[/\\]", value)):
        raise ValueError("runtime paths must not contain dot segments")
    return value


def _root_keys(value: str) -> set[tuple[str, str]]:
    """Return every valid lexical identity without guessing the remote OS.

    A forward-slash UNC spelling can also name a POSIX absolute path. Compare
    both interpretations so ambiguous spellings cannot hide conflicting roots.
    Windows comparison is conservatively case-insensitive; no filesystem is
    inspected and the supplied paths themselves are left unchanged.
    """
    keys: set[tuple[str, str]] = set()
    if PureWindowsPath(value).is_absolute():
        keys.add(("windows", PureWindowsPath(value).as_posix().rstrip("/").casefold()))
    if PurePosixPath(value).is_absolute():
        keys.add(("posix", "/" + "/".join(part for part in value.split("/") if part)))
    return keys


class WorkspaceMapping(DuplicateSafeJsonModel):
    """One host-validated logical filesystem root and its execution-side root."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    logical_root: str = Field(min_length=1, max_length=4096)
    execution_root: str = Field(min_length=1, max_length=4096)
    _paths = field_validator("logical_root", "execution_root")(_absolute_path)


class RuntimeLocation(DuplicateSafeJsonModel):
    """Runtime facts supplied by the host/backend, not inferred from tool keys."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    working_directory: str | None = Field(default=None, min_length=1, max_length=4096)
    workspace_mappings: list[WorkspaceMapping] = Field(default_factory=list, max_length=16)

    @field_validator("working_directory")
    @classmethod
    def absolute_working_directory(cls, value: str | None) -> str | None:
        return _absolute_path(value) if value is not None else None

    @model_validator(mode="after")
    def meaningful_location(self) -> RuntimeLocation:
        if self.working_directory is None and not self.workspace_mappings:
            raise ValueError("runtime context must supply a working directory or workspace mapping")
        roots: set[tuple[str, str]] = set()
        for mapping in self.workspace_mappings:
            keys = _root_keys(mapping.logical_root)
            if any(
                style == other_style
                and (
                    root == other
                    or root.startswith(other.rstrip("/") + "/")
                    or other.startswith(root.rstrip("/") + "/")
                )
                for style, root in keys
                for other_style, other in roots
            ):
                raise ValueError(
                    "runtime workspace mappings must not duplicate or overlap logical roots"
                )
            roots.update(keys)
        return self


class ToolRuntimeContext(DuplicateSafeJsonModel):
    """Live review context; an operator-trusted assertion, never execution approval."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    source: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z][A-Za-z0-9_.:-]*$")
    runtime: RuntimeLocation

    @field_validator("version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("runtime context version must be integer 1")
        return value

    @model_validator(mode="after")
    def bounded_context(self) -> ToolRuntimeContext:
        if len(self.model_dump_json().encode("utf-8")) > MAX_RUNTIME_CONTEXT_BYTES:
            raise ValueError("runtime context exceeds the byte limit")
        return self
