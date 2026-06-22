# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.


"""
Clock abstraction for testable time-dependent logic.

Following Lakos: inject time dependencies to enable deterministic testing.
"""

from abc import ABC, abstractmethod
from datetime import UTC, datetime


class Clock(ABC):
    """Abstract clock interface for time operations."""

    @abstractmethod
    def now(self) -> datetime:
        """
        Return current UTC time.

        Returns
        -------
        datetime
            The current time in UTC.
        """
        pass

    @abstractmethod
    def now_unix(self) -> float:
        """
        Return current time as Unix timestamp.

        Returns
        -------
        float
            The current Unix timestamp in seconds.
        """
        pass

    def is_expired(self, expiry: datetime) -> bool:
        """
        Check if a given expiry time has passed.

        Parameters
        ----------
        expiry : datetime
            The time to check against current time.

        Returns
        -------
        bool
            True if current time is greater than or equal to expiry, False otherwise.
        """
        return self.now() >= expiry

    def seconds_until(self, target: datetime) -> float:
        """
        Return seconds until target time (negative if past).

        Parameters
        ----------
        target : datetime
            The future (or past) target time.

        Returns
        -------
        float
            Number of seconds from now until the target time.
        """
        return (target - self.now()).total_seconds()


class SystemClock(Clock):
    """Production clock using system time."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def now_unix(self) -> float:
        return self.now().timestamp()


class MockClock(Clock):
    """
    Testable clock with controllable time.

    Usage:
        clock = MockClock(datetime(2024, 1, 1, tzinfo=timezone.utc))
        clock.advance(seconds=300)
        clock.set(datetime(2024, 6, 1, tzinfo=timezone.utc))
    """

    def __init__(self, initial: datetime | None = None):
        self._time = initial or datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._time

    def now_unix(self) -> float:
        return self._time.timestamp()

    def advance(self, seconds: float = 0, minutes: float = 0, hours: float = 0) -> None:
        """
        Advance clock by specified duration.

        Parameters
        ----------
        seconds : float, optional
            Seconds to advance, by default 0.
        minutes : float, optional
            Minutes to advance, by default 0.
        hours : float, optional
            Hours to advance, by default 0.
        """
        from datetime import timedelta

        total_seconds = seconds + (minutes * 60) + (hours * 3600)
        self._time += timedelta(seconds=total_seconds)

    def set(self, time: datetime) -> None:
        """
        Set clock to specific time.

        Parameters
        ----------
        time : datetime
            The new time to set the clock to. Must be timezone-aware.

        Raises
        ------
        ValueError
            If the provided time is not timezone-aware.
        """
        if time.tzinfo is None:
            raise ValueError("Clock time must be timezone-aware")
        self._time = time
