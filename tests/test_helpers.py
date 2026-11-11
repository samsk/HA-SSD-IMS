"""Tests for pure helper functions: date-range math, name sanitization, chart validation."""

from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.ssd_ims.helpers import (
    InvalidChartData,
    InvalidRegistryData,
    aggregate_chart_day,
    calculate_yesterday_range,
    chart_day_trends,
    expected_quarters,
    sanitize_name,
    validate_chart_structure,
    validate_registry_page,
    validate_registry_readings,
)
from custom_components.ssd_ims.models import ChartData, RegistryReading


class TestCalculateYesterdayRange:
    """calculate_yesterday_range must always return UTC-aware boundaries —
    a past bug (CHANGELOG v2.0.5) returned a naive local datetime for the
    start of the range."""

    def test_utc_input_returns_utc_aware_bounds_one_day_apart(self):
        now = datetime(2026, 6, 15, 14, 30, tzinfo=timezone.utc)

        start, end = calculate_yesterday_range(now)

        assert start.tzinfo == timezone.utc
        assert end.tzinfo == timezone.utc
        assert start == datetime(2026, 6, 14, 0, 0, tzinfo=timezone.utc)
        assert end == datetime(2026, 6, 15, 0, 0, tzinfo=timezone.utc)
        assert end - start == timedelta(days=1)

    def test_start_is_tz_aware_not_naive(self):
        now = datetime(2026, 6, 15, 14, 30, tzinfo=ZoneInfo("Europe/Bratislava"))

        start, end = calculate_yesterday_range(now)

        assert start.tzinfo is not None
        assert end.tzinfo is not None

    def test_local_midnight_boundaries_convert_correctly_in_winter(self):
        """Local midnight in winter (CET, UTC+1) must convert to 23:00 UTC
        the prior day, not be off by an hour."""
        now = datetime(2026, 1, 15, 10, 0, tzinfo=ZoneInfo("Europe/Bratislava"))

        start, end = calculate_yesterday_range(now)

        assert start == datetime(2026, 1, 13, 23, 0, tzinfo=timezone.utc)
        assert end == datetime(2026, 1, 14, 23, 0, tzinfo=timezone.utc)

    def test_local_midnight_boundaries_convert_correctly_in_summer_dst(self):
        """Local midnight in summer (CEST, UTC+2) must convert to 22:00 UTC
        the prior day."""
        now = datetime(2026, 7, 15, 10, 0, tzinfo=ZoneInfo("Europe/Bratislava"))

        start, end = calculate_yesterday_range(now)

        assert start == datetime(2026, 7, 13, 22, 0, tzinfo=timezone.utc)
        assert end == datetime(2026, 7, 14, 22, 0, tzinfo=timezone.utc)


class TestSanitizeName:
    def test_replaces_special_characters_with_underscore(self):
        assert sanitize_name("Home (Rodinný dom)") == "home_rodinn_dom"

    def test_collapses_consecutive_replacements_and_strips_edges(self):
        assert sanitize_name("  multiple   spaces  ") == "multiple_spaces"

    def test_lower_false_preserves_case(self):
        assert sanitize_name("Home Address", lower=False) == "Home_Address"

    def test_already_valid_identifier_is_unchanged(self):
        assert sanitize_name("pod_1") == "pod_1"

    def test_only_invalid_characters_sanitizes_to_empty_string(self):
        assert sanitize_name("!!!") == ""


def _chart(
    stamps: list[str],
    consumption: list[float],
    supply: list[float] | None = None,
    *,
    sum_c: float | None = None,
    sum_s: float | None = None,
) -> ChartData:
    supply = supply if supply is not None else [0.0] * len(stamps)
    if sum_c is None:
        sum_c = sum(consumption) * 0.25
    if sum_s is None:
        sum_s = sum(supply) * 0.25
    return ChartData(
        meteringDatetime=stamps,
        actualConsumption=consumption,
        actualSupply=supply,
        sumActualConsumption=sum_c,
        sumActualSupply=sum_s,
    )


class TestExpectedQuarters:
    def test_ordinary_day_has_96(self):
        tz = ZoneInfo("Europe/Bratislava")
        start = datetime(2026, 10, 22, tzinfo=tz)
        assert expected_quarters(start, start + timedelta(days=1)) == 96

    def test_dst_end_has_100(self):
        tz = ZoneInfo("Europe/Bratislava")
        start = datetime(2026, 10, 25, tzinfo=tz)
        assert expected_quarters(start, start + timedelta(days=1)) == 100

    def test_dst_start_has_92(self):
        tz = ZoneInfo("Europe/Bratislava")
        start = datetime(2026, 3, 29, tzinfo=tz)
        assert expected_quarters(start, start + timedelta(days=1)) == 92


class TestValidateChartStructure:
    def test_empty_is_valid(self):
        validate_chart_structure(ChartData())

    def test_length_mismatch_raises(self):
        with pytest.raises(InvalidChartData) as err:
            validate_chart_structure(
                _chart(
                    ["2026-10-22T22:15:00+00:00", "2026-10-22T22:30:00+00:00"],
                    [1.0],
                    [0.0, 0.0],
                )
            )
        assert err.value.reason == "length_mismatch"

    def test_gap_not_15min_raises(self):
        with pytest.raises(InvalidChartData) as err:
            validate_chart_structure(
                _chart(
                    ["2026-10-22T22:15:00+00:00", "2026-10-22T22:45:00+00:00"],
                    [1.0, 1.0],
                    [0.0, 0.0],
                )
            )
        assert err.value.reason == "gap_not_15min"

    def test_negative_raises(self):
        with pytest.raises(InvalidChartData) as err:
            validate_chart_structure(
                _chart(["2026-10-22T22:15:00+00:00"], [-1.0], [0.0])
            )
        assert err.value.reason == "negative_or_non_finite"

    def test_sum_mismatch_beyond_tolerance_warns_only(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            validate_chart_structure(
                _chart(
                    ["2026-10-22T22:15:00+00:00"],
                    [4.0],
                    [0.0],
                    sum_c=99.0,
                    sum_s=0.0,
                )
            )
        assert any("sumActual mismatch" in r.message for r in caplog.records)


class TestAggregateChartDay:
    def test_excludes_from_boundary_quarter(self):
        tz = ZoneInfo("Europe/Bratislava")
        day_start = datetime(2026, 10, 23, tzinfo=tz)
        day_end = day_start + timedelta(days=1)
        # Boundary stamp at day_start (= end of previous day's last quarter)
        # plus one in-day stamp.
        stamps = [
            day_start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            (day_start.astimezone(UTC) + timedelta(minutes=15)).strftime(
                "%Y-%m-%dT%H:%M:%S+00:00"
            ),
        ]
        chart = _chart(stamps, [4.0, 4.0], [0.0, 0.0])
        day = aggregate_chart_day(chart, chart.actual_consumption, day_start, day_end)
        assert day.quarter_count == 1
        assert day.total_kwh == pytest.approx(1.0)
        assert day.complete is False


class TestChartDayTrends:
    def test_complete_day_min_peak_hour_load_factor(self):
        tz = ZoneInfo("Europe/Bratislava")
        day_start = datetime(2026, 10, 23, tzinfo=tz)
        day_end = day_start + timedelta(days=1)
        stamps = []
        values = []
        for i in range(96):
            end = day_start + timedelta(minutes=15 * (i + 1))
            stamps.append(end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00"))
            values.append(2.0)
        # Quarter starting 03:00 (index 12) → 0 kW; 14:00 (index 56) → 8 kW
        values[12] = 0.0
        values[56] = 8.0
        chart = _chart(stamps, values, [0.0] * 96)
        trends = chart_day_trends(chart, values, day_start, day_end)
        assert trends is not None
        assert trends.low_demand_kw == 0.0
        assert trends.peak_chart_kw == 8.0
        assert trends.peak_hour == 14
        # 94×2×0.25 + 0 + 8×0.25 = 47 + 2 = 49 kWh; hours=24
        assert trends.load_factor_pct == pytest.approx((49.0 / 24) / 8 * 100)

    def test_incomplete_day_returns_none(self):
        tz = ZoneInfo("Europe/Bratislava")
        day_start = datetime(2026, 10, 23, tzinfo=tz)
        day_end = day_start + timedelta(days=1)
        stamps = [
            (day_start + timedelta(minutes=15))
            .astimezone(UTC)
            .strftime("%Y-%m-%dT%H:%M:%S+00:00")
        ]
        chart = _chart(stamps, [1.0], [0.0])
        assert chart_day_trends(chart, [1.0], day_start, day_end) is None


class TestValidateRegistry:
    def test_truncated_page(self):
        with pytest.raises(InvalidRegistryData) as err:
            validate_registry_page(10, 2)
        assert err.value.reason == "truncated_page"

    def test_vt_nt_total_mismatch(self):
        readings = [
            RegistryReading(
                serial_number="M1",
                reading_time=datetime(2026, 10, 31, 23, tzinfo=UTC),
                consumption_tariff1=10.0,
                consumption_tariff2=20.0,
                consumption_total=50.0,
            )
        ]
        with pytest.raises(InvalidRegistryData) as err:
            validate_registry_readings(readings)
        assert err.value.reason == "vt_nt_total_mismatch"
