"""Helper utilities for SSD IMS integration."""

from __future__ import annotations

import logging
import math
import re
from bisect import bisect_right
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .const import DAILY_POLL_HOUR, DAILY_POLL_MINUTE, DEBUG_SCAN_INTERVAL
from .models import ChartData, RegistryReading

_LOGGER = logging.getLogger(__name__)

QUARTER_HOURS = timedelta(minutes=15)
# Portal sumActual* vs sum(kW)*0.25: warn on large mismatch; import still uses
# the quarter arrays (sumActual* meaning was never confirmed on a live payload).
SUM_TOLERANCE_RATIO = 0.01
SUM_TOLERANCE_ABS_KWH = 0.1


class InvalidChartData(ValueError):
    """Chart payload failed a structural correctness check."""

    def __init__(self, reason: str, message: str = "") -> None:
        self.reason = reason
        super().__init__(message or reason)


class InvalidRegistryData(ValueError):
    """Registry payload failed a structural correctness check."""

    def __init__(self, reason: str, message: str = "") -> None:
        self.reason = reason
        super().__init__(message or reason)


@dataclass(frozen=True)
class DayEnergy:
    """Hourly kWh for one local day, plus completeness of the quarter series."""

    hourly_kwh: dict[datetime, float]
    quarter_count: int
    complete: bool

    @property
    def total_kwh(self) -> float:
        """Sum of all hourly buckets."""
        return sum(self.hourly_kwh.values())


def calculate_yesterday_range(now: datetime) -> tuple[datetime, datetime]:
    """Calculate date range for yesterday in the API-expected format."""
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    period_start = (today_midnight - timedelta(days=1)).astimezone(UTC)
    period_end = today_midnight.astimezone(UTC)
    return period_start, period_end


@dataclass(frozen=True)
class PollSchedule:
    """Next coordinator delay until the following portal poll."""

    delay: timedelta


def daily_poll_slot(
    now: datetime, jitter: timedelta, *, day_offset: int = 0
) -> datetime:
    """Local 03:05 + jitter for today (day_offset=0) or a relative day."""
    day = (now + timedelta(days=day_offset)).replace(
        hour=DAILY_POLL_HOUR,
        minute=DAILY_POLL_MINUTE,
        second=0,
        microsecond=0,
    )
    return day + jitter


def next_poll_delay(
    now: datetime,
    *,
    jitter: timedelta,
    yesterday_complete: bool,
    scan_interval: timedelta,
    debug: bool = False,
    debug_interval: timedelta | None = None,
) -> PollSchedule:
    """Delay until the next portal poll.

    First attempt is local 03:05 + jitter. Incomplete yesterday retries on the
    configured scan_interval with no cap. Once complete, wait for tomorrow's
    same slot. Debug mode always returns a short fixed interval.
    """
    if debug_interval is None:
        debug_interval = timedelta(minutes=DEBUG_SCAN_INTERVAL)

    if debug:
        return PollSchedule(delay=debug_interval)

    today_slot = daily_poll_slot(now, jitter)
    if now < today_slot:
        return PollSchedule(delay=today_slot - now)

    if yesterday_complete:
        tomorrow_slot = daily_poll_slot(now, jitter, day_offset=1)
        return PollSchedule(delay=tomorrow_slot - now)

    return PollSchedule(delay=scan_interval)


def sanitize_name(name: str, *, lower: bool = True) -> str:
    """Sanitize a name for use in identifiers."""
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    sanitized = re.sub(r"_+", "_", sanitized).strip("_")
    return sanitized.lower() if lower else sanitized


def expected_quarters(day_start: datetime, day_end: datetime) -> int:
    """Number of 15-minute slots in the half-open local-day window.

    Convert to UTC before subtracting so DST long/short days are counted
    correctly (plain aware subtraction keeps wall-clock length at 24h).
    """
    return int(
        (day_end.astimezone(UTC) - day_start.astimezone(UTC)).total_seconds() // 900
    )


def parse_chart_timestamp(timestamp_str: str) -> datetime:
    """Parse a portal chart timestamp into an aware UTC datetime."""
    text = timestamp_str.replace("Z", "+00:00")
    if "." in text:
        head, _, tail = text.partition(".")
        # Only the fractional digits — do not swallow the +HH:MM offset digits.
        match = re.match(r"^(\d+)(.*)$", tail)
        if match:
            digits, offset = match.group(1), match.group(2)
            text = f"{head}.{digits[:6].ljust(6, '0')}{offset}"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise InvalidChartData("naive_timestamp", f"Naive timestamp: {timestamp_str}")
    return parsed.astimezone(UTC)


def _finite_non_negative(value: float, *, field: str, index: int) -> None:
    """Raise when a numeric sample is not a finite non-negative number."""
    if not math.isfinite(value) or value < 0:
        raise InvalidChartData(
            "negative_or_non_finite",
            f"{field}[{index}]={value!r} is not a finite non-negative number",
        )


def validate_chart_structure(
    chart_data: ChartData, *, now: datetime | None = None
) -> None:
    """Raise InvalidChartData when the portal chart payload is structurally wrong.

    An empty series (portal has not published yet) is valid and returns without
    raising — incompleteness is handled by the caller.
    """
    stamps = chart_data.metering_datetime
    if not stamps:
        return

    consumption = chart_data.actual_consumption
    supply = chart_data.actual_supply
    if not (len(stamps) == len(consumption) == len(supply)):
        raise InvalidChartData(
            "length_mismatch",
            f"Array lengths differ: timestamps={len(stamps)}, "
            f"consumption={len(consumption)}, supply={len(supply)}",
        )

    now_utc = (now or datetime.now(UTC)).astimezone(UTC)
    previous: datetime | None = None
    for i, stamp_str in enumerate(stamps):
        try:
            stamp = parse_chart_timestamp(stamp_str)
        except (ValueError, TypeError) as err:
            raise InvalidChartData(
                "bad_timestamp", f"Cannot parse timestamp[{i}]: {stamp_str!r}"
            ) from err
        if stamp > now_utc + timedelta(minutes=15):
            raise InvalidChartData(
                "future_timestamp", f"Timestamp[{i}] is in the future: {stamp}"
            )
        if previous is not None:
            if stamp <= previous:
                raise InvalidChartData(
                    "non_increasing",
                    f"Timestamps are not strictly increasing at index {i}",
                )
            if stamp - previous != QUARTER_HOURS:
                raise InvalidChartData(
                    "gap_not_15min",
                    f"Gap at index {i} is {stamp - previous}, expected 15 minutes",
                )
        previous = stamp
        _finite_non_negative(consumption[i], field="actual_consumption", index=i)
        _finite_non_negative(supply[i], field="actual_supply", index=i)

    for attr, portal_sum in (
        ("actual_consumption", chart_data.sum_actual_consumption or 0.0),
        ("actual_supply", chart_data.sum_actual_supply or 0.0),
    ):
        values = getattr(chart_data, attr)
        computed = sum(values) * 0.25
        diff = abs(computed - float(portal_sum))
        if diff > SUM_TOLERANCE_ABS_KWH and diff > abs(computed) * SUM_TOLERANCE_RATIO:
            _LOGGER.warning(
                "sumActual mismatch for %s: portal=%s computed=%.6f diff=%.6f "
                "(import continues from quarter arrays)",
                attr,
                portal_sum,
                computed,
                diff,
            )


def aggregate_chart_day(
    chart_data: ChartData,
    values: list[float],
    day_start: datetime,
    day_end: datetime,
) -> DayEnergy:
    """Map quarter-hour kW samples whose *start* falls in the day to hourly kWh.

    Does not validate — call validate_chart_structure first. Incomplete days
    return complete=False without raising.
    """
    expected = expected_quarters(day_start, day_end)
    hourly: dict[datetime, float] = {}
    quarter_count = 0

    for i, timestamp_str in enumerate(chart_data.metering_datetime):
        timestamp_end_utc = parse_chart_timestamp(timestamp_str)
        timestamp_start_utc = timestamp_end_utc - QUARTER_HOURS
        if not day_start <= timestamp_start_utc < day_end:
            continue
        quarter_count += 1
        hour_timestamp = timestamp_start_utc.replace(minute=0, second=0, microsecond=0)
        hourly[hour_timestamp] = hourly.get(hour_timestamp, 0.0) + values[i] * 0.25

    return DayEnergy(
        hourly_kwh=hourly,
        quarter_count=quarter_count,
        complete=quarter_count == expected and expected > 0,
    )


@dataclass(frozen=True)
class ChartDayTrends:
    """Yesterday chart extremes and load factor (complete days only)."""

    low_demand_kw: float
    peak_chart_kw: float
    load_factor_pct: float | None
    peak_hour: int


def chart_day_trends(
    chart_data: ChartData,
    values: list[float],
    day_start: datetime,
    day_end: datetime,
) -> ChartDayTrends | None:
    """Min/max quarter kW, peak hour, load factor for a complete local day.

    Returns None when the day is incomplete or has no in-day quarters.
    """
    expected = expected_quarters(day_start, day_end)
    quarter_kws: list[float] = []
    hourly: dict[datetime, float] = {}

    for i, timestamp_str in enumerate(chart_data.metering_datetime):
        timestamp_end_utc = parse_chart_timestamp(timestamp_str)
        timestamp_start_utc = timestamp_end_utc - QUARTER_HOURS
        if not day_start <= timestamp_start_utc < day_end:
            continue
        quarter_kws.append(values[i])
        hour_timestamp = timestamp_start_utc.replace(minute=0, second=0, microsecond=0)
        hourly[hour_timestamp] = hourly.get(hour_timestamp, 0.0) + values[i] * 0.25

    if not quarter_kws or len(quarter_kws) != expected:
        return None

    low_kw = min(quarter_kws)
    peak_kw = max(quarter_kws)
    peak_hour_start = max(hourly, key=hourly.get)
    local_peak = peak_hour_start.astimezone(day_start.tzinfo)
    hour_count = len(hourly)
    total_kwh = sum(hourly.values())
    load_factor_pct = (
        (total_kwh / hour_count) / peak_kw * 100.0
        if peak_kw > 0 and hour_count
        else None
    )
    return ChartDayTrends(
        low_demand_kw=low_kw,
        peak_chart_kw=peak_kw,
        load_factor_pct=load_factor_pct,
        peak_hour=local_peak.hour,
    )


def validate_registry_readings(readings: list[RegistryReading]) -> None:
    """Raise InvalidRegistryData when register readings are structurally wrong."""
    previous: datetime | None = None
    for reading in readings:
        if not reading.serial_number:
            raise InvalidRegistryData("empty_serial", "Register row has empty serial")
        if reading.reading_time.tzinfo is None:
            raise InvalidRegistryData(
                "naive_timestamp", "Register reading_time is naive"
            )
        if previous is not None and reading.reading_time <= previous:
            raise InvalidRegistryData(
                "non_increasing",
                "Register reading times are not strictly increasing",
            )
        previous = reading.reading_time
        for attr in (
            "max_demand",
            "min_demand",
            "consumption_total",
            "consumption_tariff1",
            "consumption_tariff2",
            "supply_total",
        ):
            value = getattr(reading, attr)
            if value is None:
                continue
            if not math.isfinite(value) or value < 0:
                raise InvalidRegistryData(
                    "negative_or_non_finite",
                    f"Register {attr}={value!r} is not finite and non-negative",
                )
        vt = reading.consumption_tariff1
        nt = reading.consumption_tariff2
        total = reading.consumption_total
        if vt is not None and nt is not None and total is not None:
            if abs(total - vt - nt) >= 0.01:
                raise InvalidRegistryData(
                    "vt_nt_total_mismatch",
                    f"total={total} != vt+nt={vt + nt}",
                )


def validate_registry_page(total_rows: int, row_count: int) -> None:
    """Refuse a truncated registry page before any statistics are written."""
    if total_rows != row_count:
        raise InvalidRegistryData(
            "truncated_page",
            f"page.totalRows={total_rows} but received {row_count} rows",
        )


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
