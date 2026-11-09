"""Helper utilities for SSD IMS integration."""

import re
from bisect import bisect_right
from datetime import datetime, timedelta, timezone


def calculate_yesterday_range(now: datetime) -> tuple[datetime, datetime]:
    """Calculate date range for yesterday in the API-expected format."""
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    period_start = (today_midnight - timedelta(days=1)).astimezone(timezone.utc)
    period_end = today_midnight.astimezone(timezone.utc)
    return period_start, period_end


def sanitize_name(name: str, *, lower: bool = True) -> str:
    """Sanitize a name for use in identifiers."""
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    sanitized = re.sub(r"_+", "_", sanitized).strip("_")
    return sanitized.lower() if lower else sanitized


class PriceHistory:
    """A price per kWh over time: hourly values with a current fallback.

    at() returns the value of the latest hour starting at or before the
    given time, so a price applies from the hour it changed until the next
    change and is never applied retroactively. Times before the first known
    hour use the first known value; without any history, the current price
    is used for every hour (a fixed price from the options).
    """

    def __init__(
        self, current: float, points: list[tuple[datetime, float]] | None = None
    ) -> None:
        """Store the current price and the (hour start, price) history."""
        self.current = current
        ordered = sorted(points or [])
        self._starts = [start for start, _ in ordered]
        self._values = [value for _, value in ordered]

    def at(self, when: datetime) -> float:
        """Price valid at the given time."""
        if not self._values:
            return self.current
        index = bisect_right(self._starts, when) - 1
        return self._values[max(index, 0)]

    def __eq__(self, other: object) -> bool:
        """Compare by content (for tests and change detection)."""
        if not isinstance(other, PriceHistory):
            return NotImplemented
        return (self.current, self._starts, self._values) == (
            other.current,
            other._starts,
            other._values,
        )

    def __repr__(self) -> str:
        """Short description for logs and test output."""
        return f"PriceHistory(current={self.current}, points={len(self._values)})"
