# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for clock module.

Following Lakos: test at the lowest level first with no dependencies.
"""

from datetime import UTC, datetime

import pytest

from nomos.level_0.clock import MockClock, SystemClock


class TestSystemClock:
    """Tests for SystemClock."""

    def test_now_returns_utc_datetime(self):
        clock = SystemClock()
        now = clock.now()
        assert now.tzinfo == UTC

    def test_now_unix_returns_float(self):
        clock = SystemClock()
        ts = clock.now_unix()
        assert isinstance(ts, float)
        assert ts > 0

    def test_is_expired_with_past_time(self):
        clock = SystemClock()
        past = datetime(2020, 1, 1, tzinfo=UTC)
        assert clock.is_expired(past) is True

    def test_is_expired_with_future_time(self):
        clock = SystemClock()
        future = datetime(2030, 1, 1, tzinfo=UTC)
        assert clock.is_expired(future) is False


class TestMockClock:
    """Tests for MockClock - enables deterministic time testing."""

    def test_initial_time(self):
        initial = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)
        clock = MockClock(initial)
        assert clock.now() == initial

    def test_default_initial_time(self):
        clock = MockClock()
        assert clock.now().year == 2024
        assert clock.now().tzinfo == UTC

    def test_advance_seconds(self):
        clock = MockClock(datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC))
        clock.advance(seconds=60)
        assert clock.now().minute == 1

    def test_advance_minutes(self):
        clock = MockClock(datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC))
        clock.advance(minutes=30)
        assert clock.now().minute == 30

    def test_advance_hours(self):
        clock = MockClock(datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC))
        clock.advance(hours=2)
        assert clock.now().hour == 2

    def test_advance_combined(self):
        clock = MockClock(datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC))
        clock.advance(hours=1, minutes=30, seconds=45)
        expected = datetime(2024, 1, 1, 1, 30, 45, tzinfo=UTC)
        assert clock.now() == expected

    def test_set_time(self):
        clock = MockClock()
        new_time = datetime(2025, 12, 25, 10, 0, 0, tzinfo=UTC)
        clock.set(new_time)
        assert clock.now() == new_time

    def test_set_time_requires_timezone(self):
        clock = MockClock()
        naive_time = datetime(2025, 12, 25, 10, 0, 0)
        with pytest.raises(ValueError, match="timezone-aware"):
            clock.set(naive_time)

    def test_is_expired_deterministic(self):
        clock = MockClock(datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC))
        expiry = datetime(2024, 1, 1, 12, 5, 0, tzinfo=UTC)

        assert clock.is_expired(expiry) is False

        clock.advance(minutes=5)
        assert clock.is_expired(expiry) is True

    def test_seconds_until(self):
        clock = MockClock(datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC))
        target = datetime(2024, 1, 1, 12, 5, 0, tzinfo=UTC)

        assert clock.seconds_until(target) == 300

        clock.advance(minutes=10)
        assert clock.seconds_until(target) == -300
