"""Data coordinator for SSD IMS integration."""

import asyncio
import logging
import random
from datetime import UTC, date, datetime, timedelta
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    StatisticMeanType,
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import CALLBACK_TYPE, CoreState, Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
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
    DAILY_POLL_JITTER,
    DEBUG_SCAN_INTERVAL,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_CLOUD_LINK,
    ISSUE_IMPORT_PAUSED,
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
from .helpers import (
    InvalidChartData,
    InvalidRegistryData,
    PriceHistory,
    aggregate_chart_day,
    calculate_yesterday_range,
    chart_day_trends,
    daily_poll_slot,
    next_poll_delay,
    sanitize_name,
    validate_chart_structure,
    validate_registry_readings,
)
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
        self.hass = hass
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
        # Supplier sensors added later (e.g. the supplier integration set up
        # after today's update) are picked up from entity registry events.
        self._supplier_pod_ids: list[str] = []
        self._unsub_supplier_registry: CALLBACK_TYPE | None = None
        # Structurally invalid portal day that blocks forward import until a
        # later good payload (or a manual reimport) succeeds.
        self._import_pause: dict[str, Any] | None = None
        # Optional local day that a manual reimport must rewrite even if hours
        # already exist for it.
        self._force_reimport_day: date | None = None
        # Portal accepted the last login (connectivity binary sensor / event).
        self.cloud_link_ok = True
        # Once per coordinator lifetime: stagger the daily 03:05 slot.
        jitter_seconds = random.randint(0, int(DAILY_POLL_JITTER.total_seconds()))
        self._poll_jitter = timedelta(seconds=jitter_seconds)
        # Manual refresh (options / reimport) must not wait for 03:05.
        self._force_immediate = False
        # True after a complete calendar-yesterday import this session.
        self.yesterday_complete = False

        # Placeholder until the first refresh sets the real delay to the slot.
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(
                minutes=config.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
            config_entry=entry,
        )

    def set_cloud_link_ok(self, ok: bool) -> None:
        """Update cloud-link state; fire event and start reauth on drop only."""
        if self.cloud_link_ok == ok:
            return
        self.cloud_link_ok = ok
        self.hass.bus.async_fire(
            EVENT_CLOUD_LINK,
            {"entry_id": self.entry.entry_id, "ok": ok},
        )
        # During setup the parent coordinator may not have finished __init__
        # yet when login fails — the binary sensor reads the flag on create.
        if hasattr(self, "_listeners"):
            self.async_update_listeners()
        if not ok:
            self.entry.async_start_reauth(self.hass)

    def _is_debug_poll(self) -> bool:
        """True when the 5-minute debug interval is selected."""
        return (
            self.config.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            == DEBUG_SCAN_INTERVAL
        )

    def _apply_poll_schedule(self, *, yesterday_complete: bool) -> None:
        """Set update_interval from the daily slot / configured retry cadence."""
        now = dt_util.now()
        scan_minutes = self.config.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        schedule = next_poll_delay(
            now,
            jitter=self._poll_jitter,
            yesterday_complete=yesterday_complete,
            scan_interval=timedelta(minutes=scan_minutes),
            debug=self._is_debug_poll(),
        )
        self.update_interval = schedule.delay
        self.yesterday_complete = yesterday_complete
        _LOGGER.debug(
            "Next poll in %g minutes (yesterday_complete=%s)",
            schedule.delay.total_seconds() / 60,
            yesterday_complete,
        )

    async def request_immediate_refresh(self) -> None:
        """Refresh now, bypassing the before-slot API skip.

        Awaits a full ``async_refresh`` (not the debounced request) so callers
        such as options/reconfigure and tests know when the update finished.
        """
        self._force_immediate = True
        await self.async_refresh()

    async def update_config(self, new_config: dict[str, Any]) -> None:
        """Update coordinator configuration."""
        self.config = new_config
        # Reset smart-polling gate so the next poll performs a full update —
        # newly added/removed PODs and price changes take effect immediately.
        self._last_successful_data_date = None
        self._last_registry_fetch_date = None
        self._force_immediate = True

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from API and update statistics."""
        force_immediate = self._force_immediate
        self._force_immediate = False
        try:
            if not self.cloud_link_ok:
                _LOGGER.debug("Cloud link down — skipping API calls until reauth")
                return self.data or {}

            now = dt_util.now()
            today = now.date()

            # Smart polling: once yesterday is confirmed complete, skip further
            # API calls until the next calendar day.
            if self._last_successful_data_date == today and self.data:
                if self._reprice_requested:
                    await self._async_reprice()
                    return self.data
                _LOGGER.debug(
                    "Data already up to date for %s — skipping API calls", today
                )
                self._apply_poll_schedule(yesterday_complete=True)
                return self.data

            # Before the daily slot, do not hit the portal (manual refresh and
            # debug 5-minute mode excepted). Calendar yesterday is not published
            # yet — Yesterday sensors show 0 (or unknown if that option is on).
            today_slot = daily_poll_slot(now, self._poll_jitter)
            if not force_immediate and not self._is_debug_poll() and now < today_slot:
                _LOGGER.debug(
                    "Before daily slot %s — skipping API calls", today_slot.isoformat()
                )
                self._apply_poll_schedule(yesterday_complete=False)
                if self.data:
                    data = {pod_id: dict(pod) for pod_id, pod in self.data.items()}
                else:
                    pod_ids = self.config.get(CONF_POINT_OF_DELIVERY) or []
                    data = {pod_id: {} for pod_id in pod_ids}
                zeros = dict.fromkeys(ENABLED_SENSOR_TYPES, 0.0)
                for pod_data in data.values():
                    pod_data["aggregated_data"] = zeros
                return data

            _LOGGER.info("Starting data update for SSD IMS integration")

            if not self.pods:
                await self._discover_pods()

            pod_ids = self.config.get(CONF_POINT_OF_DELIVERY) or list(self.pods.keys())
            if not pod_ids:
                _LOGGER.warning("No PODs configured or discovered. Skipping update.")
                self._apply_poll_schedule(yesterday_complete=False)
                return {}

            # First refresh already runs in a background task after setup, so
            # it is safe to walk statistics inline (no bootstrap timeout).
            stats_complete = await self._update_statistics(pod_ids)

            all_pod_data: dict[str, Any] = {pod_id: {} for pod_id in pod_ids}
            await self._fetch_cumulative_totals_from_statistics(all_pod_data)

            now = dt_util.now()
            for pod_id in pod_ids:
                pod = self.pods.get(pod_id)
                if not pod:
                    continue

                pod_data = all_pod_data.setdefault(pod_id, {})
                previous_pod = (self.data or {}).get(pod_id, {})
                if previous_trends := previous_pod.get("trends"):
                    pod_data["trends"] = dict(previous_trends)
                if previous_agg := previous_pod.get("aggregated_data"):
                    pod_data["aggregated_data"] = previous_agg
                if previous_update := previous_pod.get("last_update"):
                    pod_data["last_update"] = previous_update

                try:
                    period_start, period_end = calculate_yesterday_range(now)
                    chart_data = await self.api_client.get_chart_data(
                        pod_id, period_start, period_end
                    )
                except SsdImsAuthenticationError:
                    raise
                except Exception as e:
                    _LOGGER.error(
                        "Error fetching yesterday data for POD %s: %s", pod_id, e
                    )
                    continue

                aggregated = self._aggregate_data(chart_data)
                if aggregated is not None:
                    pod_data["aggregated_data"] = aggregated
                    self._merge_chart_trends(pod_data, chart_data, aggregated)
                    pod_data["last_update"] = now.isoformat()

            self._reprice_requested = False
            await self._update_registry(pod_ids, all_pod_data, today)
            await self._update_consumption_trends(pod_ids, all_pod_data)
            await self._update_hourly_costs(pod_ids)
            self._watch_supplier_prices(pod_ids)

            if stats_complete:
                self._last_successful_data_date = today
                _LOGGER.debug(
                    "Statistics complete for %s — future polls today will be skipped",
                    today,
                )

            self._apply_poll_schedule(yesterday_complete=stats_complete)
            _LOGGER.info("Data update for all sensors completed.")
            return all_pod_data

        except SsdImsAuthenticationError as err:
            _LOGGER.error("Cloud link lost: %s", err)
            self.set_cloud_link_ok(False)
            return self.data or {}
        except Exception as e:
            _LOGGER.error("Error updating data: %s", e)
            raise UpdateFailed(f"Error updating data: {e}") from e

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
        concurrent callers (manual reimport and a scheduled poll can
        otherwise overlap on the same statistics).
        """
        async with self._stats_lock:
            return await self._update_statistics_locked(pod_ids)

    def _raise_import_pause(
        self, *, pod_name: str, day: date, reason: str, statistic_id: str
    ) -> None:
        """Record a pause and raise (or refresh) the import_paused repair.

        One issue id for the domain (``import_paused``). The entry id lives in
        the issue data so ``repairs.py`` can reimport; a second entry would
        overwrite the same issue — this integration is one entry per account.
        """
        self._import_pause = {
            "pod_name": pod_name,
            "day": day.isoformat(),
            "reason": reason,
            "statistic_id": statistic_id,
        }
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_IMPORT_PAUSED,
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_IMPORT_PAUSED,
            translation_placeholders={
                "pod_name": pod_name,
                "day": day.isoformat(),
                "reason": reason,
            },
            data={"entry_id": self.entry.entry_id},
        )

    def async_clear_import_pause(self) -> None:
        """Clear pause state and delete the repair issue if one exists."""
        self._import_pause = None
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_IMPORT_PAUSED)

    async def async_reimport(self, day: date | None = None) -> None:
        """Force a rewrite from one local day through yesterday.

        Default day is the paused day, else yesterday. Clears the smart-poll
        gate so the next refresh rebases from that midnight even when hours
        already exist for a closed day.
        """
        today = dt_util.now().date()
        if day is None:
            if self._import_pause and self._import_pause.get("day"):
                day = date.fromisoformat(self._import_pause["day"])
            else:
                day = today - timedelta(days=1)
        self._force_reimport_day = day
        self._last_successful_data_date = None
        self._force_immediate = True
        await self.async_request_refresh()

    async def _async_sum_before(self, statistic_id: str, before: datetime) -> float:
        """Return the cumulative sum of the last hour strictly before before."""

        def _stats() -> list[dict[str, Any]]:
            result = statistics_during_period(
                self.hass,
                before - timedelta(days=40),
                before,
                {statistic_id},
                "hour",
                None,
                {"sum"},
            )
            return result.get(statistic_id, [])

        rows = await get_instance(self.hass).async_add_executor_job(_stats)
        previous = 0.0
        for row in rows:
            start = row["start"]
            if isinstance(start, (int, float)):
                start = dt_util.utc_from_timestamp(start)
            if start < before:
                previous = row.get("sum") or 0.0
        return previous

    def _sensor_values(self, chart_data: ChartData, sensor_type: str) -> list[float]:
        """Return the kW series for a sensor type."""
        if sensor_type == SENSOR_TYPE_ACTUAL_CONSUMPTION:
            return chart_data.actual_consumption
        return chart_data.actual_supply

    async def _update_statistics_locked(self, pod_ids: list[str]) -> bool:
        """Import statistics for all configured PODs.

        Returns True when statistics are complete through yesterday for every
        configured POD and sensor type — meaning no further API calls are needed
        today and smart polling can safely skip the next scheduled update.
        """
        all_up_to_date = True
        force_day = self._force_reimport_day
        # Consumed for this run whether or not it succeeds — a later successful
        # day clear is enough to resume; a failed forced day keeps the pause.
        self._force_reimport_day = None
        # Explicit reimport (service / repair) may rewrite a closed day and
        # accept a short/empty response for that day; scheduled polls do not.
        explicit = force_day is not None

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
                end_date = dt_util.now().replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                last_pending_day = end_date - timedelta(days=1)
                last_stat_timestamp: datetime | None = None
                rewrite_from_midnight = False
                # Remember before last_stat_timestamp is cleared for a rewrite:
                # an empty day after stored history is a hole, not a leading gap.
                had_prior_stats = bool(
                    last_stats_result and statistic_id in last_stats_result
                )

                if had_prior_stats:
                    last_stat = last_stats_result[statistic_id][0]
                    cumulative_sum = last_stat.get("sum") or 0.0
                    if start_val := last_stat.get("start"):
                        if isinstance(start_val, (int, float)):
                            start_val = dt_util.utc_from_timestamp(start_val)
                        last_stat_timestamp = dt_util.as_local(start_val)
                        last_day_start = last_stat_timestamp.replace(
                            hour=0, minute=0, second=0, microsecond=0
                        )
                        # An open yesterday must be re-fetched and rewritten
                        # from its local midnight — otherwise a partial morning
                        # import would permanently close the day.
                        if last_day_start == last_pending_day:
                            start_date = last_day_start
                            rewrite_from_midnight = True
                        else:
                            start_date = last_day_start + timedelta(days=1)
                    else:
                        history_days = max(
                            self.config.get(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS),
                            1,
                        )
                        start_date = end_date - timedelta(days=history_days)
                else:
                    history_days = max(
                        self.config.get(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS),
                        1,
                    )
                    start_date = end_date - timedelta(days=history_days)

                if force_day is not None:
                    forced_start = end_date.replace(
                        year=force_day.year, month=force_day.month, day=force_day.day
                    )
                    # Explicit reimport rebases from that local midnight even
                    # when it is before the last stored hour (closed day).
                    if forced_start < end_date:
                        start_date = forced_start
                        rewrite_from_midnight = True

                if start_date >= end_date:
                    continue

                if rewrite_from_midnight:
                    cumulative_sum = await self._async_sum_before(
                        statistic_id, start_date.astimezone(UTC)
                    )
                    last_stat_timestamp = None

                got_last_day_complete = False
                imported_any = False
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
                        chart_data = await self.api_client.get_chart_data(
                            pod_id, day_start.astimezone(UTC), day_end.astimezone(UTC)
                        )

                        validate_chart_structure(chart_data, now=dt_util.now())
                        day_energy = aggregate_chart_day(
                            chart_data,
                            self._sensor_values(chart_data, sensor_type),
                            day_start,
                            day_end,
                        )

                        if day_energy.quarter_count == 0:
                            if day_start == last_pending_day and not explicit:
                                # Portal has not published yesterday yet.
                                all_up_to_date = False
                                _LOGGER.debug(
                                    "No in-day data for %s on %s — will retry",
                                    statistic_id,
                                    day_start.date(),
                                )
                                break
                            if (
                                not had_prior_stats
                                and not imported_any
                                and not explicit
                            ):
                                # Leading gap (meter not installed yet).
                                current_date += timedelta(days=1)
                                continue
                            if explicit:
                                # User asked to accept whatever the portal has.
                                current_date += timedelta(days=1)
                                continue
                            # Hole after stored history: do not skip it.
                            self._raise_import_pause(
                                pod_name=pod_name,
                                day=day_start.date(),
                                reason="empty",
                                statistic_id=statistic_id,
                            )
                            all_up_to_date = False
                            _LOGGER.error(
                                "Empty day for %s on %s after prior data — paused",
                                statistic_id,
                                day_start.date(),
                            )
                            break

                        # Short closed day on a scheduled poll: pause. Yesterday
                        # may still be written partial. Explicit reimport writes
                        # the short day as returned and continues.
                        if (
                            not day_energy.complete
                            and day_start != last_pending_day
                            and not explicit
                        ):
                            self._raise_import_pause(
                                pod_name=pod_name,
                                day=day_start.date(),
                                reason="incomplete",
                                statistic_id=statistic_id,
                            )
                            all_up_to_date = False
                            _LOGGER.error(
                                "Incomplete data for %s on %s — paused",
                                statistic_id,
                                day_start.date(),
                            )
                            break

                        if day_start == last_pending_day and day_energy.complete:
                            got_last_day_complete = True

                        day_stats = []
                        for hour_timestamp, hourly_value in sorted(
                            day_energy.hourly_kwh.items()
                        ):
                            if (
                                last_stat_timestamp is not None
                                and hour_timestamp <= last_stat_timestamp
                            ):
                                continue
                            cumulative_sum += hourly_value
                            day_stats.append(
                                {"start": hour_timestamp, "sum": cumulative_sum}
                            )

                        if day_stats:
                            async_add_external_statistics(
                                self.hass, metadata, day_stats
                            )
                            last_stat_timestamp = day_stats[-1]["start"]
                            imported_any = True
                            if (
                                self._import_pause
                                and self._import_pause.get("day")
                                == day_start.date().isoformat()
                            ):
                                self.async_clear_import_pause()

                        if day_start == last_pending_day and not day_energy.complete:
                            all_up_to_date = False
                            break

                    except InvalidChartData as err:
                        _LOGGER.error(
                            "Invalid chart data for %s on %s: %s",
                            statistic_id,
                            day_start.date(),
                            err,
                        )
                        self._raise_import_pause(
                            pod_name=pod_name,
                            day=day_start.date(),
                            reason=err.reason,
                            statistic_id=statistic_id,
                        )
                        all_up_to_date = False
                        break
                    except SsdImsAuthenticationError:
                        raise
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

                if not got_last_day_complete:
                    _LOGGER.debug(
                        "Yesterday not complete for %s (pending from %s) — will retry",
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
        *,
        rebuild_all: bool = False,
    ) -> None:
        """Import register deltas as a cumulative sum series.

        By default only the last stored bucket and anything newer are upserted,
        so a second reading that lands in an already-imported hour updates that
        hour instead of being skipped. When rebuild_all is True (monthly costs),
        the whole series is rebuilt from the deltas and price history.
        """
        last_stats_result = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, {"start", "sum"}
        )
        last_start: datetime | None = None
        if last_stats_result and statistic_id in last_stats_result:
            last_stat = last_stats_result[statistic_id][0]
            if (start_val := last_stat.get("start")) is not None:
                if isinstance(start_val, (int, float)):
                    start_val = dt_util.utc_from_timestamp(start_val)
                last_start = start_val

        cumulative_sum = 0.0
        stats = []
        for bucket, delta in deltas:
            cumulative_sum += delta
            if not rebuild_all and last_start is not None and bucket < last_start:
                continue
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
                        "min_demand": latest.min_demand,
                    }
                except InvalidRegistryData as e:
                    all_ok = False
                    _LOGGER.error("Invalid register data for POD %s: %s", pod_id, e)
                    self._raise_import_pause(
                        pod_name=pod_name,
                        day=today,
                        reason=e.reason,
                        statistic_id=self._build_statistic_id(
                            pod_name, REGISTER_STAT_CONSUMPTION_VT
                        ),
                    )
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
        validate_registry_readings(readings)
        # Read the prices only now: the options may have changed while the
        # registry request was in flight.
        price_vt, price_nt = await self._pod_price_histories(pod_id)
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
                # Rebuild the whole cost series so a later price history (e.g.
                # supplier sensors that appeared after the first import) can
                # correct earlier flat-priced buckets.
                await self._import_register_series(
                    self._build_statistic_id(pod_name, cost_key),
                    f"{pod_name} Cost {label}",
                    CURRENCY,
                    None,
                    [(b, d * price.at(b)) for b, d in deltas],
                    rebuild_all=True,
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
        self._supplier_pod_ids = list(pod_ids)
        if self._unsub_supplier_registry is None:
            self._unsub_supplier_registry = self.hass.bus.async_listen(
                er.EVENT_ENTITY_REGISTRY_UPDATED, self._async_supplier_registry_updated
            )
        registry = er.async_get(self.hass)
        entity_ids = frozenset(
            entity_id
            for pod_id in pod_ids
            for tariff in ("vt", "nt", "jt")
            if (entity_id := self._supplier_entity_id(registry, pod_id, tariff))
        )
        if entity_ids == self._supplier_entity_ids:
            return
        self._unwatch_supplier_states()
        self._supplier_entity_ids = entity_ids
        if entity_ids:
            self._unsub_supplier = async_track_state_change_event(
                self.hass, list(entity_ids), self._async_supplier_price_changed
            )

    @callback
    def async_unwatch_supplier_prices(self) -> None:
        """Stop watching the supplier price sensors and their registry."""
        if self._unsub_supplier_registry:
            self._unsub_supplier_registry()
            self._unsub_supplier_registry = None
        self._unwatch_supplier_states()

    @callback
    def _unwatch_supplier_states(self) -> None:
        """Stop watching the states of the supplier price sensors."""
        if self._unsub_supplier:
            self._unsub_supplier()
            self._unsub_supplier = None
        self._supplier_entity_ids = frozenset()

    @callback
    def _async_supplier_registry_updated(self, event: Event) -> None:
        """Watch supplier price sensors registered after the last update.

        Without this, a supplier integration set up after the day's update
        would only be used the next day, because the daily gate skips the
        full update that looks the sensors up.
        """
        entity_id = event.data["entity_id"]
        if entity_id not in self._supplier_entity_ids:
            entry = er.async_get(self.hass).async_get(entity_id)
            if entry is None or entry.platform != SUPPLIER_PRICE_DOMAIN:
                return
        before = self._supplier_entity_ids
        self._watch_supplier_prices(self._supplier_pod_ids)
        if self._supplier_entity_ids == before:
            return
        # The sensor may already have its value; otherwise the state listener
        # reprices once it gets one.
        for watched in self._supplier_entity_ids - before:
            state = self.hass.states.get(watched)
            try:
                float(state.state if state else "")
            except ValueError:
                continue
            self._request_reprice()
            return
        if before - self._supplier_entity_ids:
            # A sensor went away: price from the options again.
            self._request_reprice()

    @callback
    def _request_reprice(self) -> None:
        """Flag a repricing and request a (debounced) refresh."""
        self._reprice_requested = True
        self.entry.async_create_background_task(
            self.hass,
            self.async_request_refresh(),
            f"{DOMAIN}_reprice_refresh",
        )

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
        # Debounced by the coordinator, so VT and NT changing together cause
        # a single refresh.
        self._request_reprice()

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

    async def _pod_price_histories(
        self, pod_id: str
    ) -> tuple[PriceHistory | None, PriceHistory | None]:
        """Return the (VT, NT) price histories for a POD.

        Supplier price sensors have state_class measurement, so Home
        Assistant keeps their hourly mean in its long-term statistics. Each
        hour is priced with that mean, so a price change applies from the
        hour it happened and never retroactively. Prices from the options
        have no history and apply to every hour, as before.
        """
        price_vt, price_nt = self._pod_prices(pod_id)
        histories: dict[str, PriceHistory | None] = {
            "vt": None if price_vt is None else PriceHistory(price_vt),
            "nt": None if price_nt is None else PriceHistory(price_nt),
        }
        if price_vt is None or self._supplier_prices(pod_id) is None:
            return histories["vt"], histories["nt"]

        registry = er.async_get(self.hass)
        entity_ids = {
            tariff: entity_id
            for tariff in ("vt", "nt", "jt")
            if (entity_id := self._supplier_entity_id(registry, pod_id, tariff))
        }
        # A single-rate point's JT price is its VT price.
        if "vt" not in entity_ids and "jt" in entity_ids:
            entity_ids["vt"] = entity_ids.pop("jt")
        entity_ids.pop("jt", None)
        if not entity_ids:
            return histories["vt"], histories["nt"]

        def _means() -> dict[str, list[dict[str, Any]]]:
            return statistics_during_period(
                self.hass,
                REGISTRY_HISTORY_START,
                None,
                set(entity_ids.values()),
                "hour",
                None,
                {"mean"},
            )

        try:
            means = await get_instance(self.hass).async_add_executor_job(_means)
        except Exception as e:
            _LOGGER.warning("Price history unavailable, using current prices: %s", e)
            return histories["vt"], histories["nt"]

        for tariff, entity_id in entity_ids.items():
            current = histories[tariff]
            if current is None:
                continue
            points = [
                (self._as_datetime(row["start"]), float(row["mean"]))
                for row in means.get(entity_id, [])
                if row.get("mean") is not None
            ]
            histories[tariff] = PriceHistory(current.current, points)
        return histories["vt"], histories["nt"]

    @staticmethod
    def _as_datetime(value: Any) -> datetime:
        """Statistics rows carry timestamps or datetimes depending on the API."""
        if isinstance(value, (int, float)):
            return dt_util.utc_from_timestamp(value)
        return value

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
        # Waiting for the recorder deadlocks while HA is still starting (the
        # recorder commits only after startup finishes). Startup refresh is a
        # background task after platforms load; skip costs until HA is running.
        # Do not gate on ``self.data is None``: on the first refresh data is
        # always None until this method returns, and the daily smart-poll gate
        # would then skip later same-day refreshes — costs would never import.
        if self.hass.state is not CoreState.running:
            return
        if not any(self._pod_prices(pod_id)[0] is not None for pod_id in pod_ids):
            return

        recorder = get_instance(self.hass)
        # Make sure this run's energy imports are committed before reading
        # them back.
        await recorder.async_block_till_done()

        for pod_id in pod_ids:
            price_vt, price_nt = await self._pod_price_histories(pod_id)
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
                    price_vt,
                    price_nt,
                )
            except Exception as e:
                _LOGGER.error("Error computing hourly costs for %s: %s", cost_id, e)

    async def _import_hourly_costs(
        self,
        energy_id: str,
        cost_id: str,
        name: str,
        periods: list[tuple[datetime, datetime, float | None]],
        price_vt: PriceHistory,
        price_nt: PriceHistory,
    ) -> None:
        """Import hourly costs, each hour priced with the prices valid then."""
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
            cost_sum += change * self._blended_price(
                periods, start, price_vt.at(start), price_nt.at(start)
            )
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
        pods = await self.api_client.get_points_of_delivery()

        if not pods:
            raise RuntimeError("No points of delivery found")

        # get_points_of_delivery() already filters out any POD it couldn't
        # parse a stable ID for, so every pod here is guaranteed valid.
        self.pods = {pod.id: pod for pod in pods}

        # Seed the API client's own POD cache so it doesn't need a redundant
        # re-fetch the next time it resolves a stable POD id (e.g. during a
        # long-running statistics backfill).
        self.api_client.set_cached_pods(list(self.pods.values()))

    def _aggregate_data(self, chart_data: ChartData | None) -> dict[str, float] | None:
        """Aggregate yesterday's chart data into per-sensor-type totals.

        Uses the same start-in-day filter and kW→kWh conversion as the
        statistics import, so the Yesterday sensors match that day's
        statistic deltas (including excluding the previous day's final
        boundary quarter). Empty or incomplete unpublished yesterday becomes
        0.0 (not last-good). Structurally invalid payloads return None so the
        caller keeps the previous value.
        """
        zeros = dict.fromkeys(ENABLED_SENSOR_TYPES, 0.0)
        if chart_data is None or not chart_data.metering_datetime:
            return zeros

        now = dt_util.now()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
            days=1
        )
        day_end = day_start + timedelta(days=1)
        try:
            validate_chart_structure(chart_data, now=now)
        except InvalidChartData as err:
            _LOGGER.warning(
                "Yesterday chart data failed validation (%s); keeping previous values",
                err.reason,
            )
            return None

        totals: dict[str, float] = {}
        for sensor_type in ENABLED_SENSOR_TYPES:
            day = aggregate_chart_day(
                chart_data,
                self._sensor_values(chart_data, sensor_type),
                day_start,
                day_end,
            )
            if day.quarter_count == 0 or not day.complete:
                return zeros
            totals[sensor_type] = day.total_kwh
        return totals

    def _merge_chart_trends(
        self,
        pod_data: dict[str, Any],
        chart_data: ChartData | None,
        aggregated: dict[str, float],
    ) -> None:
        """Fill chart-based trend keys when yesterday's series is complete."""
        if chart_data is None or not chart_data.metering_datetime:
            return
        now = dt_util.now()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
            days=1
        )
        day_end = day_start + timedelta(days=1)
        trends = chart_day_trends(
            chart_data,
            self._sensor_values(chart_data, SENSOR_TYPE_ACTUAL_CONSUMPTION),
            day_start,
            day_end,
        )
        if trends is None:
            # Incomplete day — keep previous chart keys; do not overwrite.
            return
        merged = pod_data.setdefault("trends", {})
        merged["low_demand_kw"] = trends.low_demand_kw
        merged["peak_chart_kw"] = trends.peak_chart_kw
        merged["load_factor_pct"] = trends.load_factor_pct
        merged["peak_hour"] = trends.peak_hour
        cons = float(aggregated.get(SENSOR_TYPE_ACTUAL_CONSUMPTION) or 0.0)
        supply = float(aggregated.get(SENSOR_TYPE_ACTUAL_SUPPLY) or 0.0)
        if supply > 0:
            merged["export_share_pct"] = supply / (cons + supply) * 100.0
        else:
            merged.pop("export_share_pct", None)

    async def _update_consumption_trends(
        self, pod_ids: list[str], all_pod_data: dict[str, Any]
    ) -> None:
        """WoW delta and 7/30-day averages from imported hourly statistics."""
        now = dt_util.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday = (today_start - timedelta(days=1)).date()
        window_start = today_start - timedelta(days=31)

        for pod_id in pod_ids:
            if pod_id not in self.pods:
                continue
            pod_name = self.config.get(CONF_POD_NAME_MAPPING, {}).get(pod_id, pod_id)
            statistic_id = self._build_statistic_id(
                pod_name, SENSOR_TYPE_ACTUAL_CONSUMPTION
            )
            try:
                daily = await self._daily_kwh_from_statistics(
                    statistic_id, window_start, today_start
                )
            except Exception as err:
                _LOGGER.warning(
                    "Could not read trend statistics for %s: %s", statistic_id, err
                )
                continue

            trends = all_pod_data.setdefault(pod_id, {}).setdefault("trends", {})
            yesterday_kwh = daily.get(yesterday)
            prior_week = daily.get(yesterday - timedelta(days=7))
            if yesterday_kwh is not None and prior_week is not None:
                trends["wow_delta_kwh"] = yesterday_kwh - prior_week
                trends["wow_percent"] = (
                    (yesterday_kwh - prior_week) / prior_week * 100.0
                    if prior_week != 0
                    else None
                )
            else:
                trends.pop("wow_delta_kwh", None)
                trends.pop("wow_percent", None)

            for key, days in (("avg_7d_kwh", 7), ("avg_30d_kwh", 30)):
                values = [
                    daily[d]
                    for offset in range(1, days + 1)
                    if (d := (today_start - timedelta(days=offset)).date()) in daily
                ]
                if values:
                    trends[key] = sum(values) / len(values)
                else:
                    trends.pop(key, None)

    async def _daily_kwh_from_statistics(
        self,
        statistic_id: str,
        window_start: datetime,
        today_start: datetime,
    ) -> dict[date, float]:
        """Map local calendar days to kWh from cumulative hourly sums.

        A day counts only when it has hours and a prior day's last sum as
        baseline; missing days are omitted (not zero-filled).
        """
        recorder = get_instance(self.hass)
        fetch_from = window_start - timedelta(days=1)

        def _fetch() -> list[dict[str, Any]]:
            result = statistics_during_period(
                self.hass,
                fetch_from,
                today_start,
                {statistic_id},
                "hour",
                None,
                {"sum"},
            )
            return result.get(statistic_id, [])

        rows = await recorder.async_add_executor_job(_fetch)
        if not rows:
            return {}

        def _ts(value: Any) -> datetime:
            if isinstance(value, (int, float)):
                return dt_util.utc_from_timestamp(value)
            return value

        daily: dict[date, float] = {}
        last_before: float | None = None
        prev_day: date | None = None
        last_in_day: float | None = None
        window_date = window_start.date()

        for row in rows:
            start = _ts(row["start"]).astimezone(today_start.tzinfo)
            if start >= today_start:
                continue
            day = start.date()
            sum_val = float(row["sum"] or 0.0)
            if prev_day is None:
                prev_day = day
                last_in_day = sum_val
                continue
            if day != prev_day:
                if last_before is not None and last_in_day is not None:
                    if prev_day >= window_date:
                        daily[prev_day] = last_in_day - last_before
                last_before = last_in_day
                prev_day = day
            last_in_day = sum_val

        if (
            prev_day is not None
            and last_before is not None
            and last_in_day is not None
            and prev_day >= window_date
        ):
            daily[prev_day] = last_in_day - last_before
        return daily
