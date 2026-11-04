# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Bounded Draft 2020-12 schema validation with no resource retrieval."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource
from referencing.jsonschema import DRAFT202012

from nomos.level_0.types.tool_call import canonical_tool_arguments
from nomos.level_0.types.tool_definition import canonical_definition_json, snapshot_tool_schema

_DIALECT = "https://json-schema.org/draft/2020-12/schema"
# Runtime budget includes admission, serialization, interpreter startup and validation.
# Four workers per service process; callers waiting for a slot share the same budget.
_ARGUMENT_VALIDATION_SECONDS = 2.0
_WORKER_LIMIT = 4
_WORKER_SLOTS = threading.BoundedSemaphore(_WORKER_LIMIT)
_WORKER_CLEANUP_SECONDS = 0.1
_WORKER_PATH = Path(__file__).with_name("_tool_schema_worker.py").resolve()


class ToolSchemaError(ValueError):
    """Invalid offline schema or arguments; submitted values are never rendered."""


def _deny_retrieval(uri: str) -> Resource[Any]:
    raise NoSuchResource(uri)


def _checked_validator(schema: dict[str, Any]) -> Draft202012Validator:
    snapshot = snapshot_tool_schema(schema)
    if snapshot.get("type") != "object":
        raise ToolSchemaError("tool parameters require explicit type: object")
    # Seed only the current document. Even an unexpected resolver path cannot
    # fetch a file or network resource. Metaschema checks use library built-ins.
    # referencing's attrs wrapper hides these generated constructors from mypy.
    registry: Registry[Any] = Registry(retrieve=_deny_retrieval)  # type: ignore[call-arg]
    resource = Resource.from_contents(snapshot, default_specification=DRAFT202012)
    registry = registry.with_resource("", resource)
    resolver = registry.resolver("")
    pending: list[Any] = [snapshot]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, bool):
            continue
        if not isinstance(current, dict):
            raise ToolSchemaError("local reference target must be a schema")
        if current.get("$schema", _DIALECT) != _DIALECT:
            raise ToolSchemaError("tool schemas require Draft 2020-12")
        if "$id" in current:
            raise ToolSchemaError("tool schemas cannot rebase resources")
        vocabulary = current.get("$vocabulary", {})
        supported = Draft202012Validator.META_SCHEMA["$vocabulary"]
        if not isinstance(vocabulary, dict) or any(name not in supported for name in vocabulary):
            raise ToolSchemaError("tool schemas cannot declare custom vocabularies")
        Draft202012Validator.check_schema(current)
        # Use the dialect's schema-position traversal, never a recursive search
        # for keyword-looking strings inside examples/default/const/annotations.
        pending.extend(DRAFT202012.subresources_of(current))
        for keyword in ("$ref", "$dynamicRef"):
            if keyword not in current:
                continue
            reference = current[keyword]
            if not isinstance(reference, str) or not reference.startswith("#"):
                raise ToolSchemaError("tool schema references must be document-local fragments")
            target = resolver.lookup(reference).contents
            pending.append(target)
    return Draft202012Validator(snapshot, registry=registry)


def validate_tool_schema(schema: dict[str, Any]) -> None:
    """Validate all active schemas/references, including unused definitions."""
    try:
        _checked_validator(schema)
    except ToolSchemaError:
        raise
    except Exception:
        # This adapter is the error boundary for library reference/regex/schema
        # and recursion failures. Do not expose library messages with live data.
        raise ToolSchemaError("invalid offline tool schema") from None


def _stop_worker(process: subprocess.Popen[bytes]) -> None:
    """Terminate, then kill if needed, and reap within two short cleanup waits."""
    try:
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=_WORKER_CLEANUP_SECONDS)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
                process.wait(timeout=_WORKER_CLEANUP_SECONDS)
    finally:
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()


def validate_tool_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> None:
    """Validate exact offline semantics within 2s, plus at most 0.2s cleanup.

    Admission to four process-wide slots, startup and validation share the deadline.
    A static isolated Python worker receives bounded JSON and returns only 0 or 1.
    No formats, coercion, defaults, shell commands or caller-provided code execute.
    """
    deadline = time.monotonic() + _ARGUMENT_VALIDATION_SECONDS
    acquired = False
    process = None
    try:
        argument_bytes = canonical_tool_arguments(arguments)
        schema_bytes = canonical_definition_json(snapshot_tool_schema(schema)).encode("utf-8")
        payload = b"[" + schema_bytes + b"," + argument_bytes + b"]"
        acquired = _WORKER_SLOTS.acquire(timeout=max(0.0, deadline - time.monotonic()))
        if not acquired or time.monotonic() >= deadline:
            raise ToolSchemaError("tool argument validation deadline exceeded")
        # -I excludes caller cwd/PYTHONPATH/user site; the static worker
        # adds only its own installed package root. Never import caller __main__.
        process = subprocess.Popen(
            [sys.executable, "-I", str(_WORKER_PATH)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ToolSchemaError("tool argument validation deadline exceeded")
        result, _ = process.communicate(payload, timeout=remaining)
        if time.monotonic() >= deadline:
            raise ToolSchemaError("tool argument validation deadline exceeded")
        if process.returncode != 0 or result != b"1":
            raise ToolSchemaError("tool arguments failed schema validation")
    except ToolSchemaError:
        raise
    except Exception:
        # Startup, timeout, crash, protocol and validation failures all deny.
        # Never include library exceptions or submitted values in the error.
        raise ToolSchemaError("tool argument validation unavailable or failed") from None
    finally:
        try:
            if process is not None:
                _stop_worker(process)
        except Exception:
            raise ToolSchemaError("tool argument validation worker cleanup failed") from None
        finally:
            if acquired:
                _WORKER_SLOTS.release()
