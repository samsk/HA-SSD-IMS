"""Regression tests for SsdImsDataCoordinator statistics import logic."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.util import dt as dt_util

from custom_components.ssd_ims.api_client import SsdImsAuthenticationError
from custom_components.ssd_ims.const import (
    CONF_HISTORY_DAYS,
    CONF_POD_NAME_MAPPING,
    ISSUE_IMPORT_PAUSED,
)
from custom_components.ssd_ims.coordinator import SsdImsDataCoordinator
from custom_components.ssd_ims.models import ChartData, PointOfDelivery


def _make_coordinator(api_client, config):
    """Build a coordinator instance without going through HA's config-entry setup."""
    coordinator = SsdImsDataCoordinator.__new__(SsdImsDataCoordinator)
    coordinator.hass = MagicMock()
    coordinator.api_client = api_client
    coordinator.config = config
    coordinator.entry = MagicMock()
    coordinator.entry.entry_id = "test-entry"
    coordinator.entry.async_create_background_task = MagicMock()
    coordinator.pods = {}
    coordinator._last_successful_data_date = None
    coordinator._stats_lock = asyncio.Lock()
    coordinator._import_pause = None
    coordinator._force_reimport_day = None
    coordinator.data = None
    return coordinator


def _chart_data(day_start: datetime, value: float = 1.0) -> ChartData:
    """One reading stamped 10:15 into the requested day (0.25 kWh at 1 kW)."""
    stamp = (day_start + timedelta(hours=10, minutes=15)).astimezone(UTC)
    return ChartData(
        meteringDatetime=[stamp.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")],
        actualConsumption=[value],
        actualSupply=[0.0],
        idleConsumption=[0.0],
        idleSupply=[0.0],
        sumActualConsumption=value * 0.25,
        sumActualSupply=0.0,
        sumIdleConsumption=0.0,
        sumIdleSupply=0.0,
    )


async def _run_executor(fn, *args, **kwargs):
    """Stand-in for HA's recorder executor job: just call the function inline."""
    return fn(*args, **kwargs)


class TestStatisticsBackfillFailureHandling:
    """Regression tests for a mid-range day failure during the backfill walk."""

    async def test_mid_range_day_failure_stops_the_walk_and_reports_incomplete(self):
        """A day that fails partway through the range must not be skipped over.

        Before the fix, an exception on a middle day was logged and the walk
        continued to later days, letting a later day's flush land on top of a
        cumulative_sum that silently omitted the failed day's contribution. If
        the final day still succeeded, the whole POD/sensor was (incorrectly)
        reported complete, and — because resumption on the next poll is based
        on the last *persisted* statistic — the failed day's gap would never
        be retried, permanently understating the running total.
        """
        fail_day = dt_util.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        ) - timedelta(days=2)

        async def get_chart_data_side_effect(pod_id, day_start, day_end):
            if day_start.date() == fail_day.date():
                raise RuntimeError("simulated network error")
            # Complete days so incompleteness does not stop the walk first.
            return _portal_chart_data(day_start, day_end, include_from=False)

        api_client = MagicMock()
        api_client.get_chart_data = AsyncMock(side_effect=get_chart_data_side_effect)

        coordinator = _make_coordinator(
            api_client, {CONF_HISTORY_DAYS: 3, CONF_POD_NAME_MAPPING: {}}
        )

        with (
            patch(
                "custom_components.ssd_ims.coordinator.get_instance"
            ) as mock_get_instance,
            patch(
                "custom_components.ssd_ims.coordinator.get_last_statistics",
                return_value={},
            ),
            patch(
                "custom_components.ssd_ims.coordinator.async_add_external_statistics"
            ) as mock_add_stats,
            patch("custom_components.ssd_ims.coordinator.asyncio.sleep", AsyncMock()),
        ):
            mock_instance = MagicMock()
            mock_instance.async_add_executor_job = AsyncMock(side_effect=_run_executor)
            mock_get_instance.return_value = mock_instance

            result = await coordinator._update_statistics_locked(["pod_1"])

        assert result is False
        # 2 sensor types x (day 1 succeeds, day 2 raises -> walk stops before day 3).
        assert api_client.get_chart_data.call_count == 4
        # Only day 1 ever produces a flush; day 3 must never be reached/flushed.
        assert mock_add_stats.call_count == 2


def _portal_chart_data(valid_from, valid_to, *, include_from: bool) -> ChartData:
    """Simulate the portal: one 4 kW (1 kWh) reading per quarter-hour, stamped by
    its *end* time, for every stamp within the requested window.

    The upper bound is always inclusive. include_from=True also returns the
    reading stamped exactly at valid_from (which belongs to the previous day),
    modeling the most permissive boundary behavior the portal could have.
    """
    stamps = []
    stamp = valid_from if include_from else valid_from + timedelta(minutes=15)
    while stamp <= valid_to:
        stamps.append(stamp.strftime("%Y-%m-%dT%H:%M:%S.0000000Z"))
        stamp += timedelta(minutes=15)
    return ChartData(
        meteringDatetime=stamps,
        actualConsumption=[4.0] * len(stamps),
        actualSupply=[4.0] * len(stamps),
        idleConsumption=[0.0] * len(stamps),
        idleSupply=[0.0] * len(stamps),
        sumActualConsumption=float(len(stamps)),
        sumActualSupply=float(len(stamps)),
        sumIdleConsumption=0.0,
        sumIdleSupply=0.0,
    )


async def _run_import_at(
    coordinator,
    frozen_now,
    *,
    last_stats=None,
    sum_before: float = 0.0,
):
    """Run the statistics import with "now" frozen; return (result, add_stats mock)."""
    with (
        patch(
            "custom_components.ssd_ims.coordinator.dt_util.now",
            return_value=frozen_now,
        ),
        patch(
            "custom_components.ssd_ims.coordinator.get_instance"
        ) as mock_get_instance,
        patch(
            "custom_components.ssd_ims.coordinator.get_last_statistics",
            return_value={} if last_stats is None else last_stats,
        ),
        patch(
            "custom_components.ssd_ims.coordinator.async_add_external_statistics"
        ) as mock_add_stats,
        patch(
            "custom_components.ssd_ims.coordinator.SsdImsDataCoordinator._async_sum_before",
            AsyncMock(return_value=sum_before),
        ),
        patch("custom_components.ssd_ims.coordinator.asyncio.sleep", AsyncMock()),
    ):
        mock_instance = MagicMock()
        mock_instance.async_add_executor_job = AsyncMock(side_effect=_run_executor)
        mock_get_instance.return_value = mock_instance

        result = await coordinator._update_statistics_locked(["pod_1"])
    return result, mock_add_stats


class TestStatisticsDayBoundaries:
    """Regression tests for #28: the 23:45–24:00 quarter-hour was dropped daily.

    The portal stamps each quarter-hour by its end time, so the last reading of
    a day is stamped 00:00 of the next day. The import used to request each day
    up to 23:59:59 local, so that reading was never returned and hour 23 came
    out one quarter (~25 %) short.
    """

    @pytest.mark.parametrize("include_from", [False, True])
    @pytest.mark.parametrize(
        ("now", "expected_hours_per_day"),
        [
            # Ordinary days: 21–23 Oct 2026.
            ("2026-10-24T10:00:00", [24, 24, 24]),
            # DST ends 25 Oct 2026 (25-hour day).
            ("2026-10-26T10:00:00", [24, 24, 25]),
            # DST starts 29 Mar 2026 (23-hour day).
            ("2026-03-30T10:00:00", [24, 24, 23]),
        ],
    )
    async def test_every_quarter_hour_is_imported_exactly_once(
        self, now, expected_hours_per_day, include_from
    ):
        """Each local hour, including 23:00 and DST-shifted ones, gets 4 quarters."""
        tz = dt_util.get_time_zone("Europe/Bratislava")
        frozen_now = datetime.fromisoformat(now).replace(tzinfo=tz)

        api_client = MagicMock()
        api_client.get_chart_data = AsyncMock(
            side_effect=lambda pod_id, valid_from, valid_to: _portal_chart_data(
                valid_from, valid_to, include_from=include_from
            )
        )
        coordinator = _make_coordinator(
            api_client, {CONF_HISTORY_DAYS: 3, CONF_POD_NAME_MAPPING: {}}
        )

        result, mock_add_stats = await _run_import_at(coordinator, frozen_now)

        assert result is True

        flushes_by_stat_id: dict[str, list[list[dict]]] = {}
        for call in mock_add_stats.call_args_list:
            _hass, metadata, day_stats = call.args
            flushes_by_stat_id.setdefault(metadata["statistic_id"], []).append(
                day_stats
            )
        assert len(flushes_by_stat_id) == 2

        for day_flushes in flushes_by_stat_id.values():
            assert [len(day) for day in day_flushes] == expected_hours_per_day

            all_stats = [stat for day in day_flushes for stat in day]
            starts = [stat["start"] for stat in all_stats]
            assert len(starts) == len(set(starts))
            assert starts == sorted(starts)
            # Hours are contiguous: nothing missing across midnight or DST.
            assert all(
                b - a == timedelta(hours=1)
                for a, b in zip(starts, starts[1:], strict=False)
            )
            # Every hour, including 23:00, holds all four quarters (4 kWh).
            sums = [0.0] + [stat["sum"] for stat in all_stats]
            assert all(
                b - a == pytest.approx(4.0)
                for a, b in zip(sums, sums[1:], strict=False)
            )
            # The first hour is local midnight of the first imported day.
            assert dt_util.as_local(starts[0]).astimezone(tz).hour == 0

    async def test_previous_days_boundary_reading_alone_is_not_yesterdays_data(self):
        """Yesterday must stay pending until a reading *within* it is published.

        A portal that includes the reading stamped exactly at valid_from returns
        the previous day's final quarter-hour even before yesterday itself is
        published. That reading is rightly filtered out of yesterday's stats,
        and it must not mark yesterday as complete either — otherwise smart
        polling would skip the rest of the day with yesterday still missing.
        """
        tz = dt_util.get_time_zone("Europe/Bratislava")
        frozen_now = datetime(2026, 10, 24, 10, 0, tzinfo=tz)
        yesterday_start = datetime(2026, 10, 23, tzinfo=tz)

        def portal(pod_id, valid_from, valid_to):
            if valid_from == yesterday_start.astimezone(UTC):
                # Not published yet: only the boundary reading comes back.
                return _portal_chart_data(valid_from, valid_from, include_from=True)
            return _portal_chart_data(valid_from, valid_to, include_from=True)

        api_client = MagicMock()
        api_client.get_chart_data = AsyncMock(side_effect=portal)
        coordinator = _make_coordinator(
            api_client, {CONF_HISTORY_DAYS: 3, CONF_POD_NAME_MAPPING: {}}
        )

        result, mock_add_stats = await _run_import_at(coordinator, frozen_now)

        assert result is False
        # Only the two published days are flushed, per sensor type.
        assert mock_add_stats.call_count == 4


class TestBackgroundBackfillGateOrdering:
    """Regression test for the first-refresh background backfill task.

    The background task used to set the smart-polling gate
    (_last_successful_data_date) *before* calling async_request_refresh().
    Since that refresh re-enters _async_update_data, which returns early
    (without recomputing cumulative totals) whenever the gate is already
    set for today, the premature set made the refresh short-circuit and
    return the stale, pre-backfill data — leaving the Total sensors stuck
    at 0 until the gate reset the next calendar day, even though the
    backfill had just successfully populated statistics.
    """

    async def test_gate_is_not_set_before_requesting_refresh(self):
        coordinator = _make_coordinator(MagicMock(), {})
        gate_at_refresh_time = "unset"

        async def fake_request_refresh():
            nonlocal gate_at_refresh_time
            gate_at_refresh_time = coordinator._last_successful_data_date

        coordinator._update_statistics = AsyncMock(return_value=True)
        coordinator.async_request_refresh = AsyncMock(side_effect=fake_request_refresh)

        await coordinator._async_backfill_statistics(["pod_1"])

        assert gate_at_refresh_time is None


class TestStatisticIdKeying:
    """statistic_id is derived from the user-editable friendly POD name
    (falling back to pod_id when no friendly name is set). This is a
    deliberate, currently-accepted limitation, not an oversight: an
    earlier pod_id-keyed design (with a rename-on-upgrade migration path)
    ran into a Home Assistant core limitation where the statistics-rename
    primitive silently no-ops for external, non-recorder-source statistics
    like ours, making that migration path unsafe. Since no published
    version of this integration has ever used a different scheme, there is
    nothing to migrate, so the simpler friendly-name-based scheme was kept.
    Renaming a POD's friendly name does start a fresh statistics series —
    documented, not silently handled."""

    def test_statistic_id_derives_from_friendly_name(self):
        assert (
            SsdImsDataCoordinator._build_statistic_id("Home", "actual_consumption")
            == "ssd_ims:home_actual_consumption"
        )

    def test_statistic_id_changes_when_friendly_name_changes(self):
        before = SsdImsDataCoordinator._build_statistic_id("Home", "actual_consumption")
        after = SsdImsDataCoordinator._build_statistic_id(
            "Cottage", "actual_consumption"
        )
        assert before != after

    def test_statistic_id_falls_back_to_pod_id_without_a_friendly_name(self):
        pod_id = "99XXX1234560000G"
        assert (
            SsdImsDataCoordinator._build_statistic_id(pod_id, "actual_consumption")
            == "ssd_ims:99xxx1234560000g_actual_consumption"
        )


class TestPodDiscovery:
    """Regression tests for _discover_pods robustness.

    Filtering out PODs with unparseable text now happens one layer down, in
    SsdImsApiClient.get_points_of_delivery (see test_api_client.py) — every
    PointOfDelivery that exists at all is guaranteed to have a valid `.id`.
    These tests confirm the coordinator correctly trusts and uses whatever
    valid list the API client hands back.
    """

    async def test_discover_pods_builds_pods_dict_and_seeds_cache(self):
        pod_a = PointOfDelivery(text="99XXX1234560000G (Home)", value="v1")
        pod_b = PointOfDelivery(text="99YYY9876540000G (Garage)", value="v2")

        api_client = MagicMock()
        api_client.get_points_of_delivery = AsyncMock(return_value=[pod_a, pod_b])
        api_client.set_cached_pods = MagicMock()

        coordinator = _make_coordinator(api_client, {})

        await coordinator._discover_pods()

        assert coordinator.pods == {pod_a.id: pod_a, pod_b.id: pod_b}
        api_client.set_cached_pods.assert_called_once_with([pod_a, pod_b])

    async def test_authentication_error_becomes_config_entry_auth_failed(self):
        """A typed auth failure from the API client must be translated into
        ConfigEntryAuthFailed, not left to fragile string matching."""
        api_client = MagicMock()
        api_client.get_points_of_delivery = AsyncMock(
            side_effect=SsdImsAuthenticationError("Not authenticated")
        )

        coordinator = _make_coordinator(api_client, {})

        with pytest.raises(ConfigEntryAuthFailed):
            await coordinator._discover_pods()


def _partial_day(day_start: datetime, hours: int = 6) -> ChartData:
    """Complete 15-minute series for the first `hours` of a local day."""
    stamps = []
    stamp = day_start.astimezone(UTC) + timedelta(minutes=15)
    end = day_start.astimezone(UTC) + timedelta(hours=hours)
    while stamp <= end:
        stamps.append(stamp.strftime("%Y-%m-%dT%H:%M:%S.0000000Z"))
        stamp += timedelta(minutes=15)
    return ChartData(
        meteringDatetime=stamps,
        actualConsumption=[4.0] * len(stamps),
        actualSupply=[4.0] * len(stamps),
        idleConsumption=[0.0] * len(stamps),
        idleSupply=[0.0] * len(stamps),
        sumActualConsumption=float(len(stamps)),
        sumActualSupply=float(len(stamps)),
        sumIdleConsumption=0.0,
        sumIdleSupply=0.0,
    )


class TestImportSafety:
    """Gap-safe walk, history_days=0, validation pause, aggregate parity."""

    async def test_history_days_zero_still_imports_yesterday(self):
        tz = dt_util.get_time_zone("Europe/Bratislava")
        frozen_now = datetime(2026, 10, 24, 10, 0, tzinfo=tz)
        api_client = MagicMock()
        api_client.get_chart_data = AsyncMock(
            side_effect=lambda pod_id, valid_from, valid_to: _portal_chart_data(
                valid_from, valid_to, include_from=False
            )
        )
        coordinator = _make_coordinator(
            api_client, {CONF_HISTORY_DAYS: 0, CONF_POD_NAME_MAPPING: {}}
        )

        result, mock_add_stats = await _run_import_at(coordinator, frozen_now)

        assert result is True
        # Only yesterday, for both sensor types.
        assert mock_add_stats.call_count == 2

    async def test_empty_mid_range_day_stops_without_flushing_later_days(self):
        tz = dt_util.get_time_zone("Europe/Bratislava")
        frozen_now = datetime(2026, 10, 24, 10, 0, tzinfo=tz)
        empty_day = datetime(2026, 10, 22, tzinfo=tz)

        def portal(pod_id, valid_from, valid_to):
            if valid_from == empty_day.astimezone(UTC):
                return ChartData()
            return _portal_chart_data(valid_from, valid_to, include_from=False)

        api_client = MagicMock()
        api_client.get_chart_data = AsyncMock(side_effect=portal)
        coordinator = _make_coordinator(
            api_client, {CONF_HISTORY_DAYS: 3, CONF_POD_NAME_MAPPING: {}}
        )

        with patch(
            "custom_components.ssd_ims.coordinator.ir.async_create_issue"
        ) as mock_issue:
            result, mock_add_stats = await _run_import_at(coordinator, frozen_now)

        assert result is False
        # Day 21 Oct succeeds (2 sensors); empty 22 Oct stops before 23 Oct.
        assert mock_add_stats.call_count == 2
        assert coordinator._import_pause["reason"] == "empty"
        assert mock_issue.call_args.args[2] == ISSUE_IMPORT_PAUSED

    async def test_empty_after_prior_stats_pauses_without_later_flush(self):
        tz = dt_util.get_time_zone("Europe/Bratislava")
        frozen_now = datetime(2026, 10, 24, 10, 0, tzinfo=tz)
        prior_hour = datetime(2026, 10, 21, 23, 0, tzinfo=tz)

        def portal(pod_id, valid_from, valid_to):
            local_day = dt_util.as_local(valid_from).date()
            if local_day.isoformat() == "2026-10-22":
                return ChartData()
            return _portal_chart_data(valid_from, valid_to, include_from=False)

        api_client = MagicMock()
        api_client.get_chart_data = AsyncMock(side_effect=portal)
        coordinator = _make_coordinator(
            api_client, {CONF_HISTORY_DAYS: 3, CONF_POD_NAME_MAPPING: {}}
        )
        last_stats = {
            "ssd_ims:pod_1_actual_consumption": [{"start": prior_hour, "sum": 10.0}],
            "ssd_ims:pod_1_actual_supply": [{"start": prior_hour, "sum": 10.0}],
        }

        with patch(
            "custom_components.ssd_ims.coordinator.ir.async_create_issue"
        ) as mock_issue:
            result, mock_add_stats = await _run_import_at(
                coordinator, frozen_now, last_stats=last_stats
            )

        assert result is False
        assert mock_add_stats.call_count == 0
        assert coordinator._import_pause["reason"] == "empty"
        assert mock_issue.call_args.args[2] == ISSUE_IMPORT_PAUSED

    async def test_leading_empty_days_still_import_later_days(self):
        tz = dt_util.get_time_zone("Europe/Bratislava")
        frozen_now = datetime(2026, 10, 24, 10, 0, tzinfo=tz)
        first_data = datetime(2026, 10, 22, tzinfo=tz)

        def portal(pod_id, valid_from, valid_to):
            if valid_from < first_data.astimezone(UTC):
                return ChartData()
            return _portal_chart_data(valid_from, valid_to, include_from=False)

        api_client = MagicMock()
        api_client.get_chart_data = AsyncMock(side_effect=portal)
        coordinator = _make_coordinator(
            api_client, {CONF_HISTORY_DAYS: 3, CONF_POD_NAME_MAPPING: {}}
        )

        result, mock_add_stats = await _run_import_at(coordinator, frozen_now)

        assert result is True
        # 22 + 23 Oct × 2 sensors.
        assert mock_add_stats.call_count == 4
        assert coordinator._import_pause is None

    async def test_short_closed_day_pauses_without_later_flush(self):
        tz = dt_util.get_time_zone("Europe/Bratislava")
        frozen_now = datetime(2026, 10, 24, 10, 0, tzinfo=tz)
        short_day = datetime(2026, 10, 22, tzinfo=tz)

        def portal(pod_id, valid_from, valid_to):
            if valid_from == short_day.astimezone(UTC):
                return _partial_day(short_day, hours=6)
            return _portal_chart_data(valid_from, valid_to, include_from=False)

        api_client = MagicMock()
        api_client.get_chart_data = AsyncMock(side_effect=portal)
        coordinator = _make_coordinator(
            api_client, {CONF_HISTORY_DAYS: 3, CONF_POD_NAME_MAPPING: {}}
        )

        with patch(
            "custom_components.ssd_ims.coordinator.ir.async_create_issue"
        ) as mock_issue:
            result, mock_add_stats = await _run_import_at(coordinator, frozen_now)

        assert result is False
        # Day 21 Oct succeeds (2 sensors); short 22 Oct stops before 23 Oct.
        assert mock_add_stats.call_count == 2
        assert coordinator._import_pause["reason"] == "incomplete"
        assert mock_issue.call_args.args[2] == ISSUE_IMPORT_PAUSED

    async def test_partial_yesterday_keeps_gate_open_without_repair(self):
        tz = dt_util.get_time_zone("Europe/Bratislava")
        frozen_now = datetime(2026, 10, 24, 10, 0, tzinfo=tz)
        yesterday = datetime(2026, 10, 23, tzinfo=tz)

        def portal(pod_id, valid_from, valid_to):
            if valid_from == yesterday.astimezone(UTC):
                return _partial_day(yesterday, hours=6)
            return _portal_chart_data(valid_from, valid_to, include_from=False)

        api_client = MagicMock()
        api_client.get_chart_data = AsyncMock(side_effect=portal)
        coordinator = _make_coordinator(
            api_client, {CONF_HISTORY_DAYS: 2, CONF_POD_NAME_MAPPING: {}}
        )

        with patch(
            "custom_components.ssd_ims.coordinator.ir.async_create_issue"
        ) as mock_issue:
            result, mock_add_stats = await _run_import_at(coordinator, frozen_now)

        assert result is False
        assert mock_add_stats.call_count >= 2
        assert coordinator._import_pause is None
        assert not mock_issue.called

    async def test_invalid_chart_data_pauses_without_writing(self):
        tz = dt_util.get_time_zone("Europe/Bratislava")
        frozen_now = datetime(2026, 10, 24, 10, 0, tzinfo=tz)
        bad_day = datetime(2026, 10, 22, tzinfo=tz)

        def portal(pod_id, valid_from, valid_to):
            if valid_from == bad_day.astimezone(UTC):
                # Length mismatch: two stamps, one consumption value.
                return ChartData(
                    meteringDatetime=[
                        (bad_day.astimezone(UTC) + timedelta(minutes=15)).strftime(
                            "%Y-%m-%dT%H:%M:%S.0000000Z"
                        ),
                        (bad_day.astimezone(UTC) + timedelta(minutes=30)).strftime(
                            "%Y-%m-%dT%H:%M:%S.0000000Z"
                        ),
                    ],
                    actualConsumption=[4.0],
                    actualSupply=[4.0, 4.0],
                    sumActualConsumption=1.0,
                    sumActualSupply=2.0,
                )
            return _portal_chart_data(valid_from, valid_to, include_from=False)

        api_client = MagicMock()
        api_client.get_chart_data = AsyncMock(side_effect=portal)
        coordinator = _make_coordinator(
            api_client, {CONF_HISTORY_DAYS: 3, CONF_POD_NAME_MAPPING: {}}
        )

        with patch(
            "custom_components.ssd_ims.coordinator.ir.async_create_issue"
        ) as mock_issue:
            result, mock_add_stats = await _run_import_at(coordinator, frozen_now)

        assert result is False
        # Day 21 Oct flushed for both sensors; bad day writes nothing; later skipped.
        assert mock_add_stats.call_count == 2
        assert coordinator._import_pause is not None
        assert coordinator._import_pause["reason"] == "length_mismatch"
        assert mock_issue.call_args.args[2] == ISSUE_IMPORT_PAUSED

    async def test_sum_mismatch_does_not_pause(self):
        tz = dt_util.get_time_zone("Europe/Bratislava")
        frozen_now = datetime(2026, 10, 24, 10, 0, tzinfo=tz)
        mismatch_day = datetime(2026, 10, 22, tzinfo=tz)

        def portal(pod_id, valid_from, valid_to):
            chart = _portal_chart_data(valid_from, valid_to, include_from=False)
            if valid_from == mismatch_day.astimezone(UTC):
                return chart.model_copy(
                    update={
                        "sum_actual_consumption": 9999.0,
                        "sum_actual_supply": 9999.0,
                    }
                )
            return chart

        api_client = MagicMock()
        api_client.get_chart_data = AsyncMock(side_effect=portal)
        coordinator = _make_coordinator(
            api_client, {CONF_HISTORY_DAYS: 3, CONF_POD_NAME_MAPPING: {}}
        )

        with patch(
            "custom_components.ssd_ims.coordinator.ir.async_create_issue"
        ) as mock_issue:
            result, mock_add_stats = await _run_import_at(coordinator, frozen_now)

        assert result is True
        assert mock_add_stats.call_count == 6
        assert coordinator._import_pause is None
        assert not mock_issue.called

    async def test_explicit_reimport_rewrites_closed_day_through_yesterday(self):
        tz = dt_util.get_time_zone("Europe/Bratislava")
        frozen_now = datetime(2026, 10, 24, 10, 0, tzinfo=tz)
        # Last stored hour is end of yesterday — 21 Oct is a closed day.
        prior_hour = datetime(2026, 10, 23, 23, 0, tzinfo=tz)
        force_day = prior_hour.date() - timedelta(days=2)  # 21 Oct

        api_client = MagicMock()
        api_client.get_chart_data = AsyncMock(
            side_effect=lambda pod_id, valid_from, valid_to: _portal_chart_data(
                valid_from, valid_to, include_from=False
            )
        )
        coordinator = _make_coordinator(
            api_client, {CONF_HISTORY_DAYS: 1, CONF_POD_NAME_MAPPING: {}}
        )
        coordinator._force_reimport_day = force_day
        last_stats = {
            "ssd_ims:pod_1_actual_consumption": [{"start": prior_hour, "sum": 100.0}],
            "ssd_ims:pod_1_actual_supply": [{"start": prior_hour, "sum": 100.0}],
        }

        result, mock_add_stats = await _run_import_at(
            coordinator, frozen_now, last_stats=last_stats, sum_before=50.0
        )

        assert result is True
        # 21 + 22 + 23 Oct × 2 sensors.
        assert mock_add_stats.call_count == 6
        assert coordinator._force_reimport_day is None
        # First hour of the rewrite should continue from sum_before (4 kWh/h).
        first_stats = mock_add_stats.call_args_list[0].args[2]
        assert first_stats[0]["sum"] == pytest.approx(54.0)

    async def test_aggregate_matches_hourly_kwh_excluding_boundary(self):
        tz = dt_util.get_time_zone("Europe/Bratislava")
        day_start = datetime(2026, 10, 23, tzinfo=tz)
        day_end = day_start + timedelta(days=1)
        chart = _portal_chart_data(
            day_start.astimezone(UTC), day_end.astimezone(UTC), include_from=True
        )
        coordinator = _make_coordinator(MagicMock(), {})

        with patch(
            "custom_components.ssd_ims.coordinator.dt_util.now",
            return_value=datetime(2026, 10, 24, 10, 0, tzinfo=tz),
        ):
            aggregated = coordinator._aggregate_data(chart)

        # 96 in-day quarters × 4 kW × 0.25 = 96 kWh (boundary excluded).
        assert aggregated["actual_consumption"] == pytest.approx(96.0)
        assert aggregated["actual_supply"] == pytest.approx(96.0)
