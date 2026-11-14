# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Transport failures of the stores a request needs before an intent exists."""

from __future__ import annotations

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

# PostgreSQL is an optional installation extra; Redis-only deployments must
# still import the service without it.
DEPENDENCY_TRANSPORT_ERRORS: tuple[type[Exception], ...] = (
    RedisConnectionError,
    RedisTimeoutError,
)
try:
    from psycopg import InterfaceError, OperationalError
    from psycopg_pool import PoolTimeout
except ImportError:
    pass
else:
    DEPENDENCY_TRANSPORT_ERRORS += (InterfaceError, OperationalError, PoolTimeout)


class DependencyUnavailableError(RuntimeError):
    """A pre-intent availability failure with a payload-safe public message."""

    def __init__(self, dependency: str):
        self.dependency = dependency
        super().__init__(f"{dependency} service unavailable; request was not admitted")
