"""Clock abstraction for testable time-dependent logic."""

from .clock import Clock, MockClock, SystemClock

__all__ = ["Clock", "MockClock", "SystemClock"]
