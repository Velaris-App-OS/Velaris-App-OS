"""Business calendar for SLA duration calculations.

Supports configurable work days, work hours, and holiday exclusions.
When computing SLA deadlines, only business hours count.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

logger = logging.getLogger(__name__)


@dataclass
class BusinessCalendar:
    """A business calendar with work days, hours, and holidays."""

    name: str = "default"
    timezone_name: str = "UTC"
    work_days: list[int] = field(default_factory=lambda: [1, 2, 3, 4, 5])
    work_start_hour: int = 9
    work_end_hour: int = 17
    holidays: list[str] = field(default_factory=list)

    @property
    def work_hours_per_day(self) -> int:
        return self.work_end_hour - self.work_start_hour

    def is_work_day(self, d: date) -> bool:
        """Check if a date is a working day (not weekend, not holiday)."""
        if d.isoweekday() not in self.work_days:
            return False
        if d.isoformat() in self.holidays:
            return False
        return True

    def is_work_time(self, dt: datetime) -> bool:
        """Check if a datetime falls within work hours on a work day."""
        if not self.is_work_day(dt.date()):
            return False
        return self.work_start_hour <= dt.hour < self.work_end_hour

    def add_business_duration(
        self, start: datetime, duration: timedelta
    ) -> datetime:
        """Add a duration counting only business hours.

        E.g., adding 8 business hours starting Friday 4pm with
        Mon-Fri 9-17 calendar → Monday 5pm (skips weekend).
        """
        remaining_seconds = duration.total_seconds()
        if remaining_seconds <= 0:
            return start

        current = start

        # If starting outside work hours, advance to next work start
        if not self.is_work_time(current):
            current = self._next_work_start(current)

        while remaining_seconds > 0:
            if not self.is_work_day(current.date()):
                current = self._next_work_start(current)
                continue

            # How many seconds left in today's work hours?
            if self.work_end_hour >= 24:
                day_end = current.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            else:
                day_end = current.replace(hour=self.work_end_hour, minute=0, second=0, microsecond=0)
            available = (day_end - current).total_seconds()

            if available <= 0:
                # Past work hours today, move to next work day
                current = self._next_work_start(
                    current + timedelta(days=1)
                )
                continue

            if remaining_seconds <= available:
                current = current + timedelta(seconds=remaining_seconds)
                remaining_seconds = 0
            else:
                remaining_seconds -= available
                current = self._next_work_start(
                    current.replace(hour=0, minute=0, second=0)
                    + timedelta(days=1)
                )

        return current

    def business_seconds_between(
        self, start: datetime, end: datetime
    ) -> float:
        """Count business seconds between two datetimes."""
        if end <= start:
            return 0.0

        total = 0.0
        current = start

        # Advance to work hours if needed
        if not self.is_work_time(current):
            current = self._next_work_start(current)

        while current < end:
            if not self.is_work_day(current.date()):
                current = self._next_work_start(
                    current + timedelta(days=1)
                )
                continue

            if self.work_end_hour >= 24:
                day_end = current.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            else:
                day_end = current.replace(hour=self.work_end_hour, minute=0, second=0, microsecond=0)

            if day_end > end:
                day_end = end

            available = (day_end - current).total_seconds()
            if available > 0:
                total += available

            current = self._next_work_start(
                current.replace(hour=0, minute=0, second=0)
                + timedelta(days=1)
            )

        return total

    def _next_work_start(self, dt: datetime) -> datetime:
        """Find the next work-day start at or after dt."""
        candidate = dt.replace(
            hour=self.work_start_hour, minute=0, second=0, microsecond=0
        )
        if candidate <= dt:
            candidate += timedelta(days=1)
            candidate = candidate.replace(
                hour=self.work_start_hour, minute=0, second=0, microsecond=0
            )

        # Skip non-work days
        safety = 0
        while not self.is_work_day(candidate.date()) and safety < 365:
            candidate += timedelta(days=1)
            safety += 1

        return candidate

    @classmethod
    def from_db_model(cls, model) -> BusinessCalendar:
        """Create from a BusinessCalendarModel row."""
        return cls(
            name=model.name,
            timezone_name=model.timezone,
            work_days=model.work_days or [1, 2, 3, 4, 5],
            work_start_hour=model.work_start_hour or 9,
            work_end_hour=model.work_end_hour or 17,
            holidays=[
                h if isinstance(h, str) else h.get("date", "")
                for h in (model.holidays or [])
            ],
        )

    @classmethod
    def twenty_four_seven(cls) -> BusinessCalendar:
        """24/7 calendar (no exclusions — same as no calendar)."""
        return cls(
            name="24x7",
            work_days=[1, 2, 3, 4, 5, 6, 7],
            work_start_hour=0,
            work_end_hour=24,
            holidays=[],
        )
