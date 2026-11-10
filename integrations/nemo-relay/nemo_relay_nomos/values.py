# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Configuration value references: literal, environment, file, encrypted (TODO).

Any configuration leaf may be written as a reference object instead of a plain
value. Errors name the field and the source, never a resolved value.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from typing import Any

MAX_FILE_BYTES = 65536
_SHAPES = (
    frozenset({"value"}),
    frozenset({"env"}),
    frozenset({"file"}),
    frozenset({"encrypted", "key_env"}),
    frozenset({"encrypted", "key_file"}),
)


class ConfigReferenceError(ValueError):
    def __init__(self, field: str, message: str):
        super().__init__(f"{field}: {message}")
        self.field, self.message = field, message


def is_reference(value: Any) -> bool:
    return isinstance(value, dict) and frozenset(value) in _SHAPES


def resolve(value: Any, *, field: str, environ: Mapping[str, str] | None = None) -> Any:
    if not is_reference(value):
        return value
    if "value" in value:
        return value["value"]
    if "encrypted" in value:
        # TODO(nomos-plugin): decrypt with a key from key_env/key_file once a scheme is chosen.
        raise ConfigReferenceError(field, "encrypted configuration values are not supported yet")
    if "env" in value:
        return _from_env(value["env"], field, os.environ if environ is None else environ)
    return _from_file(value["file"], field)


def resolve_tree(config: Any, *, environ: Mapping[str, str] | None = None, path: str = "") -> Any:
    if is_reference(config):
        return resolve(config, field=path or "<root>", environ=environ)
    if isinstance(config, dict):
        return {
            k: resolve_tree(v, environ=environ, path=f"{path}.{k}" if path else str(k))
            for k, v in config.items()
        }
    if isinstance(config, list):
        return [resolve_tree(v, environ=environ, path=f"{path}[{i}]") for i, v in enumerate(config)]
    return config


def _from_env(name: Any, field: str, environ: Mapping[str, str]) -> str:
    if not isinstance(name, str) or not name:
        raise ConfigReferenceError(field, "env reference must name a variable")
    value = environ.get(name)
    if not value:
        raise ConfigReferenceError(
            field,
            f"environment variable {name} is not set in this process. Managed Relay workers start with a "
            "minimal environment (PATH, TMPDIR, SSL_CERT_*), so use a file reference or a literal there.",
        )
    return value


def _from_file(path: Any, field: str) -> str:
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ConfigReferenceError(field, "file reference must be an absolute path")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)  # a FIFO must not block startup
    except OSError as exc:
        raise ConfigReferenceError(field, f"cannot open file reference {path} ({exc.strerror})") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ConfigReferenceError(field, f"file reference {path} is not a regular file")
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ConfigReferenceError(
                field, f"file reference {path} must be owned by this user with mode 0600 or 0400"
            )
        raw = os.read(fd, MAX_FILE_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_FILE_BYTES:
        raise ConfigReferenceError(field, f"file reference {path} exceeds {MAX_FILE_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigReferenceError(field, f"file reference {path} is not UTF-8") from None
    return text[:-1] if text.endswith("\n") else text
