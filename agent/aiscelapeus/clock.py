"""Time as an injected dependency.

Triage timestamps, escalation request times and the tier state machine's dwell
windows are all decided by "what time is it now". Left as a direct call to
`datetime.now`, none of them can be asserted deterministically and the tier
machine's 2- and 5-second windows can only be tested with real sleeps.

Deliberately not freezegun: it patches time globally, which would also freeze
the `perf_counter` timers that measure the 10ms Moss retrieval budget. A frozen
clock makes every latency assertion pass while measuring nothing - the exact
failure mode this codebase is trying to eliminate. Injecting a clock keeps the
domain deterministic and leaves real measurement real.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Wall time for the record, monotonic time for durations."""

    def now(self) -> datetime:
        """Current UTC time. Used for anything written into the record."""
        ...

    def monotonic_ms(self) -> float:
        """Monotonic milliseconds. Used for durations and dwell windows."""
        ...


class SystemClock:
    """The real clock. The default everywhere outside tests."""

    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic_ms(self) -> float:
        return time.perf_counter() * 1000.0


class ManualClock:
    """A clock that only moves when a test moves it.

    Wall time and monotonic time advance together, so a test that advances by
    three seconds sees both a three-second timestamp delta and a 3000ms
    monotonic delta - which is what the tier machine's dwell windows compare.
    """

    __slots__ = ("_now", "_monotonic_ms")

    def __init__(self, start: datetime | None = None) -> None:
        if start is None:
            start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        if start.tzinfo is None:
            raise ValueError("ManualClock requires a timezone-aware start time")
        self._now = start.astimezone(timezone.utc)
        self._monotonic_ms = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic_ms(self) -> float:
        return self._monotonic_ms

    def advance(self, seconds: float) -> None:
        """Move both clocks forward. Negative advances are a test bug."""
        if seconds < 0:
            raise ValueError("time does not run backwards; advance requires seconds >= 0")
        self._now += timedelta(seconds=seconds)
        self._monotonic_ms += seconds * 1000.0


SYSTEM_CLOCK = SystemClock()


def isoformat(moment: datetime) -> str:
    """The one timestamp format written into the record."""
    return moment.isoformat(timespec="milliseconds")
