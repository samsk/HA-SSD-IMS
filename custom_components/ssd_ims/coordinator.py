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
    statistics_during_period,
    StatisticMeanType,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import CALLBACK_TYPE, CoreState, Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
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
    CONF_PRICE_NT,
    CONF_PRICE_VT,
    CONF_SCAN_INTERVAL,
    COST_RECOMPUTE_PERIODS,
    CURRENCY,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    REGISTER_STAT_CONSUMPTION_NT,
    REGISTER_STAT_CONSUMPTION_VT,
    REGISTER_STAT_COST_NT,
    REGISTER_STAT_COST_VT,
    REGISTRY_HISTORY_START,
    SENSOR_TYPE_ACTUAL_CONSUMPTION,
    SENSOR_TYPE_ACTUAL_SUPPLY,
    STAT_ACTUAL_CONSUMPTION_COST,
    SUPPLIER_PRICE_DOMAIN,
)
from .helpers import calculate_yesterday_range, sanitize_name
from .models import ChartData, PointOfDelivery, RegistryReading

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
        # Register readings change at most a few times a month, so they're
        # fetched at most once per calendar day, independently of the
        # profile-data smart-polling gate.
        self._registry_lock = asyncio.Lock()
        self._last_registry_fetch_date: date | None = None
        # Latest fetched register readings per POD, for pricing hourly costs
        self._registry_readings: dict[str, list[RegistryReading]] = {}
        # Supplier price sensors being watched, and the listener watching them.
        # A price change requests a refresh that only reprices (see
        # _async_update_data), without new portal requests.
        self._supplier_entity_ids: frozenset[str] = frozenset()
        self._unsub_supplier: CALLBACK_TYPE | None = None
        self._reprice_requested = False

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
        # Prices may have changed — re-run the register import on next poll.
        self._last_registry_fetch_date = None

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from API and update statistics."""
        try:
            today = dt_util.now().date()

            # Smart polling: the portal publishes data once per day after midnight.
            # Once we have confirmed that all statistics are current, skip further
            # API calls until the next calendar day.
            if self._last_successful_data_date == today and self.data:
                if self._reprice_requested:
                    await self._async_reprice()
                    return self.data
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

            self._reprice_requested = False
            await self._update_registry(pod_ids, all_pod_data, today)
            await self._update_hourly_costs(pod_ids)
            self._watch_supplier_prices(pod_ids)

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

    @staticmethod
    def _registry_bucket(reading_time: datetime) -> datetime:
        """Return the statistics hour a register delta ending at reading_time
        belongs to.

        A reading stamped exactly on the hour (the usual 00:00 local reading
        on the 1st) closes the *previous* hour — so the month's consumption
        lands in that month, not the next one. An ad-hoc reading at e.g.
        18:21 belongs to the 18:00 hour.
        """
        reading_utc = reading_time.astimezone(UTC)
        floored = reading_utc.replace(minute=0, second=0, microsecond=0)
        if floored == reading_utc:
            return floored - timedelta(hours=1)
        return floored

    @classmethod
    def _registry_deltas(
        cls, readings: list[RegistryReading], attr: str
    ) -> list[tuple[datetime, float]]:
        """Turn cumulative register readings into (hour, delta) pairs.

        No delta is produced across a meter replacement (serial number
        change) or a counter that went backwards, since the difference
        between two unrelated counters is meaningless. Deltas falling into
        the same hour are merged.
        """
        merged: dict[datetime, float] = {}
        previous: tuple[str, float] | None = None
        for reading in sorted(readings, key=lambda r: r.reading_time):
            value = getattr(reading, attr)
            if value is None:
                continue
            if previous is not None:
                prev_serial, prev_value = previous
                if reading.serial_number != prev_serial or value < prev_value:
                    # No serial numbers in the log: they identify the meter.
                    _LOGGER.info(
                        "Register %s reset at %s (%s); skipping delta across it",
                        attr,
                        reading.reading_time,
                        "meter replaced"
                        if reading.serial_number != prev_serial
                        else "counter decreased",
                    )
                else:
                    bucket = cls._registry_bucket(reading.reading_time)
                    merged[bucket] = merged.get(bucket, 0.0) + (value - prev_value)
            previous = (reading.serial_number, value)
        return sorted(merged.items())

    async def _import_register_series(
        self,
        statistic_id: str,
        name: str,
        unit: str,
        unit_class: str | None,
        deltas: list[tuple[datetime, float]],
    ) -> None:
        """Append register deltas newer than the last imported statistic."""
        last_stats_result = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, {"start", "sum"}
        )
        cumulative_sum = 0.0
        last_start: datetime | None = None
        if last_stats_result and statistic_id in last_stats_result:
            last_stat = last_stats_result[statistic_id][0]
            cumulative_sum = last_stat.get("sum") or 0.0
            if (start_val := last_stat.get("start")) is not None:
                if isinstance(start_val, (int, float)):
                    start_val = dt_util.utc_from_timestamp(start_val)
                last_start = start_val

        stats = []
        for bucket, delta in deltas:
            if last_start is not None and bucket <= last_start:
                continue
            cumulative_sum += delta
            stats.append({"start": bucket, "sum": round(cumulative_sum, 6)})

        if not stats:
            return

        metadata = {
            "has_sum": True,
            "mean_type": StatisticMeanType.NONE,
            "name": name,
            "source": DOMAIN,
            "statistic_id": statistic_id,
            "unit_of_measurement": unit,
            "unit_class": unit_class,
        }
        async_add_external_statistics(self.hass, metadata, stats)
        _LOGGER.debug(
            "Imported %d register statistics for %s", len(stats), statistic_id
        )

    async def _update_registry(
        self, pod_ids: list[str], all_pod_data: dict[str, Any], today: date
    ) -> None:
        """Fetch tariff register readings and import VT/NT energy + cost stats.

        Runs at most once per calendar day. On days it doesn't run (or a POD
        fails), the previously fetched latest reading is carried over so the
        register sensors keep their value.
        """
        previous_data = self.data or {}
        for pod_id in pod_ids:
            if previous := previous_data.get(pod_id, {}).get("registry"):
                all_pod_data.setdefault(pod_id, {})["registry"] = previous

        if self._last_registry_fetch_date == today:
            return

        async with self._registry_lock:
            now = dt_util.now()
            all_ok = True

            for pod_id in pod_ids:
                if pod_id not in self.pods:
                    continue
                pod_name = self.config.get(CONF_POD_NAME_MAPPING, {}).get(
                    pod_id, pod_id
                )
                try:
                    readings = await self.api_client.get_registry_data(
                        pod_id, REGISTRY_HISTORY_START, now + timedelta(days=1)
                    )
                    if not readings:
                        continue
                    self._registry_readings[pod_id] = readings
                    await self._import_register_stats(pod_id, pod_name, readings)

                    latest = readings[-1]
                    all_pod_data.setdefault(pod_id, {})["registry"] = {
                        "reading_time": latest.reading_time.isoformat(),
                        "serial_number": latest.serial_number,
                        "consumption_vt": latest.consumption_tariff1,
                        "consumption_nt": latest.consumption_tariff2,
                        "consumption_total": latest.consumption_total,
                        "max_demand": latest.max_demand,
                    }
                except Exception as e:
                    all_ok = False
                    _LOGGER.error(
                        "Error fetching register readings for POD %s: %s", pod_id, e
                    )

            if all_ok:
                self._last_registry_fetch_date = today

    async def _import_register_stats(
        self, pod_id: str, pod_name: str, readings: list[RegistryReading]
    ) -> None:
        """Import the monthly VT/NT energy and, when priced, cost statistics."""
        # Read the prices only now: the options may have changed while the
        # registry request was in flight, and imported buckets are never
        # repriced later.
        price_vt, price_nt = self._pod_prices(pod_id)
        for attr, energy_key, cost_key, label, price in (
            (
                "consumption_tariff1",
                REGISTER_STAT_CONSUMPTION_VT,
                REGISTER_STAT_COST_VT,
                "VT",
                price_vt,
            ),
            (
                "consumption_tariff2",
                REGISTER_STAT_CONSUMPTION_NT,
                REGISTER_STAT_COST_NT,
                "NT",
                price_nt,
            ),
        ):
            deltas = self._registry_deltas(readings, attr)
            await self._import_register_series(
                self._build_statistic_id(pod_name, energy_key),
                f"{pod_name} Consumption {label}",
                "kWh",
                "energy",
                deltas,
            )
            if price is not None:
                # Buckets skipped while the price was unknown are imported on
                # a later run: only buckets newer than the cost series' own
                # last statistic are appended.
                await self._import_register_series(
                    self._build_statistic_id(pod_name, cost_key),
                    f"{pod_name} Cost {label}",
                    CURRENCY,
                    None,
                    [(b, d * price) for b, d in deltas],
                )

    async def _async_reprice(self) -> None:
        """Recompute costs from cached readings after a supplier price change."""
        self._reprice_requested = False
        pod_ids = list(self._registry_readings)
        _LOGGER.debug("Supplier price changed, recomputing costs")
        async with self._registry_lock:
            for pod_id in pod_ids:
                pod_name = self.config.get(CONF_POD_NAME_MAPPING, {}).get(
                    pod_id, pod_id
                )
                try:
                    await self._import_register_stats(
                        pod_id, pod_name, self._registry_readings[pod_id]
                    )
                except Exception as e:
                    _LOGGER.error("Error importing register costs: %s", e)
        await self._update_hourly_costs(pod_ids)

    def _watch_supplier_prices(self, pod_ids: list[str]) -> None:
        """Request a repricing refresh when a supplier price sensor changes."""
        registry = er.async_get(self.hass)
        entity_ids = frozenset(
            entity_id
            for pod_id in pod_ids
            for tariff in ("vt", "nt", "jt")
            if (entity_id := self._supplier_entity_id(registry, pod_id, tariff))
        )
        if entity_ids == self._supplier_entity_ids:
            return
        self.async_unwatch_supplier_prices()
        self._supplier_entity_ids = entity_ids
        if entity_ids:
            self._unsub_supplier = async_track_state_change_event(
                self.hass, list(entity_ids), self._async_supplier_price_changed
            )

    @callback
    def async_unwatch_supplier_prices(self) -> None:
        """Stop watching the supplier price sensors."""
        if self._unsub_supplier:
            self._unsub_supplier()
            self._unsub_supplier = None
        self._supplier_entity_ids = frozenset()

    @callback
    def _async_supplier_price_changed(self, event: Event) -> None:
        """Flag a repricing when a supplier price gets a new numeric value."""
        old, new = event.data.get("old_state"), event.data.get("new_state")
        if new is None or (old is not None and old.state == new.state):
            return
        try:
            float(new.state)
        except ValueError:
            return
        self._reprice_requested = True
        # Debounced by the coordinator, so VT and NT changing together cause
        # a single refresh.
        self.hass.async_create_task(self.async_request_refresh())

    @staticmethod
    def _supplier_entity_id(
        registry: er.EntityRegistry, pod_id: str, tariff: str
    ) -> str | None:
        """Entity ID of the supplier's price sensor for a POD and tariff."""
        return registry.async_get_entity_id(
            Platform.SENSOR,
            SUPPLIER_PRICE_DOMAIN,
            f"ELECTRICITY_{pod_id}_price_{tariff}",
        )

    def _pod_prices(self, pod_id: str) -> tuple[float | None, float | None]:
        """Return the (VT, NT) price per kWh incl. VAT for a POD.

        Prices from the supplier integration's sensors for the same EIC win
        over the prices entered in the options. When the supplier has
        sensors for the POD but they have no value yet (e.g. right after a
        restart), no price is returned rather than a possibly stale manual
        one; the costs are then computed on a later run.
        """
        supplier = self._supplier_prices(pod_id)
        if supplier is not None:
            return supplier
        price_vt = self.config.get(CONF_PRICE_VT)
        price_nt = self.config.get(CONF_PRICE_NT)
        return (
            None if price_vt is None else float(price_vt),
            None if price_nt is None else float(price_nt),
        )

    def _supplier_prices(self, pod_id: str) -> tuple[float | None, float | None] | None:
        """Read the supplier integration's price sensors for a POD.

        Returns None when the supplier has no price sensors for this EIC,
        and (None, None) while any of them has no value: pricing night
        consumption with the VT price just because the NT sensor is
        unavailable would be wrong.
        """
        registry = er.async_get(self.hass)
        entity_ids = {
            tariff: self._supplier_entity_id(registry, pod_id, tariff)
            for tariff in ("vt", "nt", "jt")
        }
        if not any(entity_ids.values()):
            return None

        def _value(tariff: str) -> float | None:
            if not (entity_id := entity_ids[tariff]):
                return None
            state = self.hass.states.get(entity_id)
            try:
                return float(state.state) if state else None
            except (TypeError, ValueError):
                return None

        values = {t: _value(t) for t, entity_id in entity_ids.items() if entity_id}
        if None in values.values():
            _LOGGER.debug("Supplier price sensors have no value yet")
            return None, None
        return values.get("vt", values.get("jt")), values.get("nt")

    @staticmethod
    def _register_periods(
        readings: list[RegistryReading],
    ) -> list[tuple[datetime, datetime, float | None]]:
        """Return (start, end, vt_share) for each pair of consecutive readings.

        vt_share is the VT fraction of the period's consumption, or None when
        nothing was consumed. Periods across a meter replacement or a counter
        that went backwards are skipped.
        """
        periods: list[tuple[datetime, datetime, float | None]] = []
        ordered = [
            r
            for r in sorted(readings, key=lambda r: r.reading_time)
            if r.consumption_tariff1 is not None and r.consumption_tariff2 is not None
        ]
        for prev, cur in zip(ordered, ordered[1:], strict=False):
            d_vt = cur.consumption_tariff1 - prev.consumption_tariff1
            d_nt = cur.consumption_tariff2 - prev.consumption_tariff2
            if cur.serial_number != prev.serial_number or d_vt < 0 or d_nt < 0:
                continue
            total = d_vt + d_nt
            periods.append(
                (
                    prev.reading_time.astimezone(UTC),
                    cur.reading_time.astimezone(UTC),
                    d_vt / total if total > 0 else None,
                )
            )
        return periods

    @staticmethod
    def _blended_price(
        periods: list[tuple[datetime, datetime, float | None]],
        hour_start: datetime,
        price_vt: float,
        price_nt: float,
    ) -> float:
        """Price per kWh for an hour, weighted by its register period's VT/NT mix.

        An hour belongs to the period its end falls into (a 00:00 reading
        closes the 23:00 hour). Hours outside all known periods — typically
        the current month, before its closing reading — use the nearest
        period's mix; periods without consumption fall back to the nearest
        earlier period that has one.
        """
        hour_end = hour_start + timedelta(hours=1)
        index = len(periods) - 1
        for i, (start, end, _) in enumerate(periods):
            if hour_end <= end:
                index = i if hour_end > start or i == 0 else i - 1
                break
        share = None
        for i in range(index, -1, -1):
            if periods[i][2] is not None:
                share = periods[i][2]
                break
        if share is None:
            share = next((p[2] for p in periods if p[2] is not None), 0.0)
        return share * price_vt + (1 - share) * price_nt

    async def _update_hourly_costs(self, pod_ids: list[str]) -> None:
        """Import an hourly cost statistic for actual_consumption.

        Lets the Energy dashboard use the hourly consumption statistic with
        costs: each hour's kWh x the blended VT/NT price of its register
        period. Costs from COST_RECOMPUTE_PERIODS register periods back are
        recomputed on every run, which corrects hours that were priced
        provisionally before their period's closing reading was published.
        Existing rows are overwritten in place by the recorder.
        """
        # Waiting for the recorder below deadlocks while Home Assistant is
        # still starting (the recorder doesn't commit until startup is done,
        # and startup waits for this integration's first refresh). Costs are
        # computed on the refresh the background backfill requests instead.
        if self.data is None or self.hass.state is not CoreState.running:
            return
        if not any(self._pod_prices(pod_id)[0] is not None for pod_id in pod_ids):
            return

        recorder = get_instance(self.hass)
        # Make sure this run's energy imports are committed before reading
        # them back.
        await recorder.async_block_till_done()

        for pod_id in pod_ids:
            price_vt, price_nt = self._pod_prices(pod_id)
            if price_vt is None:
                continue
            if price_nt is None:
                # Single-rate tariff (D1/D2): all consumption is at the VT price.
                price_nt = price_vt
            readings = self._registry_readings.get(pod_id)
            periods = self._register_periods(readings or [])
            if not periods:
                continue
            pod_name = self.config.get(CONF_POD_NAME_MAPPING, {}).get(pod_id, pod_id)
            energy_id = self._build_statistic_id(
                pod_name, SENSOR_TYPE_ACTUAL_CONSUMPTION
            )
            cost_id = self._build_statistic_id(pod_name, STAT_ACTUAL_CONSUMPTION_COST)
            try:
                await self._import_hourly_costs(
                    energy_id,
                    cost_id,
                    f"{pod_name} Actual Consumption Cost",
                    periods,
                    float(price_vt),
                    float(price_nt),
                )
            except Exception as e:
                _LOGGER.error("Error computing hourly costs for %s: %s", cost_id, e)

    async def _import_hourly_costs(
        self,
        energy_id: str,
        cost_id: str,
        name: str,
        periods: list[tuple[datetime, datetime, float | None]],
        price_vt: float,
        price_nt: float,
    ) -> None:
        recorder = get_instance(self.hass)

        def _stats(statistic_id: str, start: datetime) -> list[dict[str, Any]]:
            result = statistics_during_period(
                self.hass, start, None, {statistic_id}, "hour", None, {"sum"}
            )
            return result.get(statistic_id, [])

        def _ts(value: Any) -> datetime:
            if isinstance(value, (int, float)):
                return dt_util.utc_from_timestamp(value)
            return value

        last_cost = await recorder.async_add_executor_job(
            get_last_statistics, self.hass, 1, cost_id, True, {"start", "sum"}
        )
        # Default: a full computation from the first energy hour
        recompute_from = REGISTRY_HISTORY_START
        cost_sum = 0.0
        if last_cost and cost_id in last_cost:
            last_start = _ts(last_cost[cost_id][0]["start"])
            period_index = max(0, len(periods) - COST_RECOMPUTE_PERIODS)
            recompute_from = min(
                last_start + timedelta(hours=1),
                periods[period_index][0].replace(minute=0, second=0, microsecond=0),
            )
            window_start = recompute_from - timedelta(days=40)
            previous = [
                row
                for row in await recorder.async_add_executor_job(
                    _stats, cost_id, window_start
                )
                if _ts(row["start"]) < recompute_from
            ]
            if previous:
                cost_sum = previous[-1]["sum"] or 0.0
            else:
                # No cost row shortly before the recompute point: start over
                recompute_from = REGISTRY_HISTORY_START

        # Fetch one extra earlier window so the first recomputed hour has a
        # predecessor to diff its cumulative energy sum against.
        fetch_from = (
            recompute_from
            if recompute_from == REGISTRY_HISTORY_START
            else recompute_from - timedelta(days=40)
        )
        energy_rows = await recorder.async_add_executor_job(
            _stats, energy_id, fetch_from
        )
        if recompute_from != REGISTRY_HISTORY_START and not any(
            _ts(row["start"]) < recompute_from for row in energy_rows
        ):
            # Without an earlier energy row the first hour's change can't be
            # derived from the cumulative sum — recompute everything instead.
            recompute_from = REGISTRY_HISTORY_START
            cost_sum = 0.0
            energy_rows = await recorder.async_add_executor_job(
                _stats, energy_id, recompute_from
            )

        stats = []
        previous_sum = 0.0
        for row in energy_rows:
            start = _ts(row["start"])
            energy_sum = row["sum"] or 0.0
            change = energy_sum - previous_sum
            previous_sum = energy_sum
            if start < recompute_from:
                continue
            cost_sum += change * self._blended_price(periods, start, price_vt, price_nt)
            stats.append({"start": start, "sum": round(cost_sum, 6)})

        if not stats:
            return
        metadata = {
            "has_sum": True,
            "mean_type": StatisticMeanType.NONE,
            "name": name,
            "source": DOMAIN,
            "statistic_id": cost_id,
            "unit_of_measurement": CURRENCY,
            "unit_class": None,
        }
        async_add_external_statistics(self.hass, metadata, stats)
        _LOGGER.debug(
            "Imported %d hourly cost statistics for %s from %s",
            len(stats),
            cost_id,
            recompute_from,
        )

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
