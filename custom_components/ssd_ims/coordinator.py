"""Data coordinator for SSD IMS integration."""

import asyncio
import logging
import random
from datetime import UTC, date, datetime, timedelta
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    StatisticMeanType,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .api_client import SsdImsApiClient, SsdImsAuthenticationError
from .const import (
    API_DELAY_MAX,
    API_DELAY_MIN,
    CONF_HISTORY_DAYS,
    CONF_POD_NAME_MAPPING,
    CONF_POINT_OF_DELIVERY,
    CONF_SCAN_INTERVAL,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    SENSOR_TYPE_ACTUAL_CONSUMPTION,
    SENSOR_TYPE_ACTUAL_SUPPLY,
)
from .helpers import calculate_yesterday_range, sanitize_name
from .models import ChartData, PointOfDelivery

_LOGGER = logging.getLogger(__name__)

# Sensor types that are always enabled for statistics import
ENABLED_SENSOR_TYPES: tuple[str, ...] = (
    SENSOR_TYPE_ACTUAL_CONSUMPTION,
    SENSOR_TYPE_ACTUAL_SUPPLY,
)


class SsdImsDataCoordinator(DataUpdateCoordinator):
    """Data coordinator for SSD IMS integration."""

    def __init__(
        self,
        hass: HomeAssistant,
        api_client: SsdImsApiClient,
        config: dict[str, Any],
        entry: ConfigEntry,
    ) -> None:
        """Initialize coordinator."""
        self.api_client = api_client
        self.config = config
        self.entry = entry
        self.pods: dict[str, PointOfDelivery] = {}
        self._last_successful_data_date: date | None = None
        self._stats_lock = asyncio.Lock()

        scan_interval = timedelta(
            minutes=config.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=scan_interval,
            config_entry=entry,
        )

    async def update_config(self, new_config: dict[str, Any]) -> None:
        """Update coordinator configuration."""
        self.config = new_config
        new_interval = timedelta(
            minutes=new_config.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )
        if self.update_interval != new_interval:
            self.update_interval = new_interval
            _LOGGER.info(
                "Update interval changed to %g minutes",
                new_interval.total_seconds() / 60,
            )
        # Reset smart-polling gate so the next scheduled poll performs a full
        # update — ensures newly added/removed PODs are picked up immediately
        # even when the config changes on the same calendar day.
        self._last_successful_data_date = None

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from API and update statistics."""
        try:
            today = dt_util.now().date()

            # Smart polling: the portal publishes data once per day after midnight.
            # Once we have confirmed that all statistics are current, skip further
            # API calls until the next calendar day.
            if self._last_successful_data_date == today and self.data:
                _LOGGER.debug(
                    "Data already up to date for %s — skipping API calls", today
                )
                return self.data

            _LOGGER.info("Starting data update for SSD IMS integration")

            if not self.pods:
                await self._discover_pods()

            pod_ids = self.config.get(CONF_POINT_OF_DELIVERY) or list(self.pods.keys())
            if not pod_ids:
                _LOGGER.warning("No PODs configured or discovered. Skipping update.")
                return {}

            if self.data is None:
                # First refresh (runs synchronously during config entry setup,
                # bounded by Home Assistant's bootstrap timeout): don't block
                # setup on a potentially large statistics backfill. Run it as
                # a background task instead and let it — plus subsequent
                # scheduled polls, which HA already runs in the background —
                # catch up incrementally.
                stats_complete = False
                self.entry.async_create_background_task(
                    self.hass,
                    self._async_backfill_statistics(pod_ids),
                    f"{DOMAIN}_initial_statistics_backfill",
                )
            else:
                stats_complete = await self._update_statistics(pod_ids)

            all_pod_data: dict[str, Any] = {pod_id: {} for pod_id in pod_ids}
            await self._fetch_cumulative_totals_from_statistics(all_pod_data)

            now = dt_util.now()
            for pod_id in pod_ids:
                pod = self.pods.get(pod_id)
                if not pod:
                    continue

                try:
                    period_start, period_end = calculate_yesterday_range(now)
                    chart_data = await self.api_client.get_chart_data(
                        pod_id, period_start, period_end
                    )
                except Exception as e:
                    _LOGGER.error(
                        "Error fetching yesterday data for POD %s: %s", pod_id, e
                    )
                    continue

                pod_data = all_pod_data.setdefault(pod_id, {})
                pod_data.update(
                    {
                        "aggregated_data": self._aggregate_data(chart_data),
                        "last_update": now.isoformat(),
                    }
                )

            if stats_complete:
                self._last_successful_data_date = today
                _LOGGER.debug(
                    "Statistics complete for %s — future polls today will be skipped",
                    today,
                )

            _LOGGER.info("Data update for all sensors completed.")
            return all_pod_data

        except ConfigEntryAuthFailed:
            raise
        except Exception as e:
            _LOGGER.error("Error updating data: %s", e)
            raise UpdateFailed(f"Error updating data: {e}") from e

    async def _async_backfill_statistics(self, pod_ids: list[str]) -> None:
        """Run the statistics backfill outside the config entry setup path.

        Started as a background task from the first refresh so a large
        catch-up range can't block Home Assistant's bootstrap timeout.
        """
        try:
            await self._update_statistics(pod_ids)
        except Exception:
            _LOGGER.exception("Error during background statistics backfill")
            return

        # Pull the newly imported totals into coordinator.data and notify
        # entities instead of waiting for the next scheduled poll. This
        # re-runs _async_update_data, which sets the smart-polling gate
        # itself once it confirms completeness — setting it here first
        # would make that refresh short-circuit on the gate and return the
        # stale pre-backfill data instead of picking up the new totals.
        await self.async_request_refresh()

    def _get_random_api_delay(self) -> float:
        """Get random API delay."""
        return random.uniform(API_DELAY_MIN, API_DELAY_MAX)

    @staticmethod
    def _build_statistic_id(pod_name: str, sensor_type: str) -> str:
        """Build the external statistic id for a POD/sensor type.

        Keyed off the user-editable friendly POD name: renaming a POD via
        the config flow changes its statistic_id and starts a fresh series
        (see CHANGELOG for the history behind this — the alternative,
        keying off the stable pod_id with a rename-on-migration path, ran
        into a Home Assistant core limitation where the statistics-rename
        primitive silently no-ops for external, non-recorder-source
        statistics like ours). No published version of this integration
        has ever used a different scheme, so there's nothing to migrate.
        """
        return f"{DOMAIN}:{sanitize_name(pod_name)}_{sensor_type}".lower()

    async def _update_statistics(self, pod_ids: list[str]) -> bool:
        """Import statistics for all configured PODs, serialized against
        concurrent callers (the background backfill task and a regular
        scheduled poll can otherwise overlap on the same statistics).
        """
        async with self._stats_lock:
            return await self._update_statistics_locked(pod_ids)

    async def _update_statistics_locked(self, pod_ids: list[str]) -> bool:
        """
        Import statistics for all configured PODs.

        Returns True when statistics are complete through yesterday for every
        configured POD and sensor type — meaning no further API calls are needed
        today and smart polling can safely skip the next scheduled update.
        """
        all_up_to_date = True
        for pod_id in pod_ids:
            pod_name_mapping = self.config.get(CONF_POD_NAME_MAPPING, {})
            pod_name = pod_name_mapping.get(pod_id, pod_id)

            for sensor_type in ENABLED_SENSOR_TYPES:
                statistic_id = self._build_statistic_id(pod_name, sensor_type)

                last_stats_result = await get_instance(
                    self.hass
                ).async_add_executor_job(
                    get_last_statistics,
                    self.hass,
                    1,
                    statistic_id,
                    True,
                    {"start", "sum"},
                )

                cumulative_sum = 0.0
                start_date = dt_util.now().replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                last_stat_timestamp = None

                if last_stats_result and statistic_id in last_stats_result:
                    last_stat = last_stats_result[statistic_id][0]
                    cumulative_sum = last_stat.get("sum") or 0.0
                    if start_val := last_stat.get("start"):
                        if isinstance(start_val, (int, float)):
                            start_val = dt_util.utc_from_timestamp(start_val)
                        last_stat_timestamp = dt_util.as_local(start_val)
                        start_date = last_stat_timestamp.replace(
                            hour=0, minute=0, second=0, microsecond=0
                        ) + timedelta(days=1)
                else:
                    history_days = self.config.get(
                        CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS
                    )
                    start_date = start_date - timedelta(days=history_days)

                end_date = dt_util.now().replace(
                    hour=0, minute=0, second=0, microsecond=0
                )

                if start_date >= end_date:
                    continue

                # Track whether the portal has published data through the most
                # recent pending day (yesterday). Earlier days in the range may
                # legitimately have data while the latest day doesn't yet, so
                # completeness is judged on that last day specifically, not on
                # whether any day in the range returned data.
                last_pending_day = end_date - timedelta(days=1)
                got_last_day_data = False
                metadata = {
                    "has_sum": True,
                    "mean_type": StatisticMeanType.NONE,
                    "name": f"{pod_name} {sensor_type.replace('_', ' ').title()}",
                    "source": DOMAIN,
                    "statistic_id": statistic_id,
                    "unit_of_measurement": "kWh",
                    "unit_class": "energy",
                }
                current_date = start_date
                while current_date < end_date:
                    day_start = current_date
                    # Exclusive end (next local midnight). The portal stamps each
                    # quarter-hour by its *end* time, so the 23:45–24:00 reading
                    # is stamped 00:00 of the next day — ending the request at
                    # 23:59:59 silently dropped it every day (#28).
                    day_end = day_start + timedelta(days=1)

                    try:
                        await asyncio.sleep(self._get_random_api_delay())
                        # Convert to UTC before calling the API, matching
                        # helpers.calculate_yesterday_range — day_start/day_end
                        # themselves stay local for the comparisons below.
                        chart_data = await self.api_client.get_chart_data(
                            pod_id, day_start.astimezone(UTC), day_end.astimezone(UTC)
                        )

                        if not chart_data or not chart_data.metering_datetime:
                            current_date += timedelta(days=1)
                            continue

                        hourly_data: dict[datetime, float] = {}
                        for i, timestamp_str in enumerate(chart_data.metering_datetime):
                            timestamp_end_utc = datetime.fromisoformat(
                                timestamp_str.replace("Z", "+00:00")
                            )
                            timestamp_start_utc = timestamp_end_utc - timedelta(
                                minutes=15
                            )
                            # Only count quarter-hours that *start* within this
                            # day, so a reading at the request boundary is never
                            # attributed to two days.
                            if not day_start <= timestamp_start_utc < day_end:
                                continue
                            # Judged after the boundary filter: the previous
                            # day's final reading alone must not mark the
                            # pending day as published.
                            if day_start == last_pending_day:
                                got_last_day_data = True

                            value = (
                                chart_data.actual_consumption[i]
                                if sensor_type == SENSOR_TYPE_ACTUAL_CONSUMPTION
                                else chart_data.actual_supply[i]
                            )
                            if value is None:
                                continue

                            hour_timestamp = timestamp_start_utc.replace(
                                minute=0, second=0, microsecond=0
                            )

                            if hour_timestamp not in hourly_data:
                                hourly_data[hour_timestamp] = 0.0
                            hourly_data[hour_timestamp] += value * 0.25

                        day_stats = []
                        for hour_timestamp, hourly_value in sorted(hourly_data.items()):
                            if (
                                last_stat_timestamp
                                and hour_timestamp <= last_stat_timestamp
                            ):
                                continue

                            cumulative_sum += hourly_value
                            day_stats.append(
                                {
                                    "start": hour_timestamp,
                                    "sum": cumulative_sum,
                                }
                            )

                        # Flush after each day so progress survives a cancelled
                        # or interrupted update instead of being redone from
                        # scratch on the next poll.
                        if day_stats:
                            async_add_external_statistics(
                                self.hass, metadata, day_stats
                            )
                            last_stat_timestamp = day_stats[-1]["start"]
                    except Exception as e:
                        _LOGGER.error(
                            "Failed to fetch or process data for %s on %s: %s",
                            statistic_id,
                            day_start.date(),
                            e,
                        )
                        # Stop walking forward for this POD/sensor type instead
                        # of skipping the failed day: later days would otherwise
                        # get flushed on top of a cumulative_sum missing this
                        # day's contribution, and since the next poll resumes
                        # from the last *persisted* statistic, the gap would
                        # never be retried and would permanently understate
                        # the running total.
                        all_up_to_date = False
                        break

                    current_date += timedelta(days=1)

                if not got_last_day_data:
                    # Portal has not published yesterday's data yet
                    _LOGGER.debug(
                        "No data available yet for %s (pending from %s) — will retry",
                        statistic_id,
                        start_date.date(),
                    )
                    all_up_to_date = False

        return all_up_to_date

    async def _fetch_cumulative_totals_from_statistics(
        self, pod_data_dict: dict[str, Any]
    ) -> None:
        """Fetch cumulative totals from external statistics."""
        for pod_id, pod_data in pod_data_dict.items():
            pod_name_mapping = self.config.get(CONF_POD_NAME_MAPPING, {})
            pod_name = pod_name_mapping.get(pod_id, pod_id)

            if "cumulative_totals" not in pod_data:
                pod_data["cumulative_totals"] = {}

            for sensor_type in ENABLED_SENSOR_TYPES:
                statistic_id = self._build_statistic_id(pod_name, sensor_type)

                last_stats = await get_instance(self.hass).async_add_executor_job(
                    get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
                )

                if last_stats and statistic_id in last_stats:
                    last_stat = last_stats[statistic_id][0]
                    cumulative_total = last_stat.get("sum", 0.0)
                    pod_data["cumulative_totals"][sensor_type] = cumulative_total
                else:
                    pod_data["cumulative_totals"][sensor_type] = 0.0

    async def _discover_pods(self) -> None:
        """Discover points of delivery."""
        try:
            pods = await self.api_client.get_points_of_delivery()
        except SsdImsAuthenticationError as e:
            raise ConfigEntryAuthFailed(
                "Authentication failed during POD discovery"
            ) from e

        if not pods:
            raise RuntimeError("No points of delivery found")

        # get_points_of_delivery() already filters out any POD it couldn't
        # parse a stable ID for, so every pod here is guaranteed valid.
        self.pods = {pod.id: pod for pod in pods}

        # Seed the API client's own POD cache so it doesn't need a redundant
        # re-fetch the next time it resolves a stable POD id (e.g. during a
        # long-running statistics backfill).
        self.api_client.set_cached_pods(list(self.pods.values()))

    def _aggregate_data(self, chart_data: ChartData | None) -> dict[str, float]:
        """Aggregate yesterday's chart data into per-sensor-type totals."""
        if chart_data is None:
            return dict.fromkeys(ENABLED_SENSOR_TYPES, 0.0)

        return {
            SENSOR_TYPE_ACTUAL_CONSUMPTION: chart_data.sum_actual_consumption or 0.0,
            SENSOR_TYPE_ACTUAL_SUPPLY: chart_data.sum_actual_supply or 0.0,
        }
