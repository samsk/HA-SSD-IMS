"""Data coordinator for SSD IMS integration."""
import logging
from datetime import datetime, timedelta
from typing import Any, Dict
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (DataUpdateCoordinator,
                                                      UpdateFailed)

from .api_client import SsdImsApiClient
from .const import (CONF_POINT_OF_DELIVERY, DEFAULT_POINT_OF_DELIVERY,
                    DEFAULT_SCAN_INTERVAL, DOMAIN,
                    SENSOR_TYPE_ACTUAL_CONSUMPTION, SENSOR_TYPE_ACTUAL_SUPPLY,
                    SENSOR_TYPE_IDLE_CONSUMPTION, SENSOR_TYPE_IDLE_SUPPLY,
                    SENSOR_TYPES)
from .models import ChartData, PointOfDelivery

_LOGGER = logging.getLogger(__name__)


class SsdImsDataCoordinator(DataUpdateCoordinator):
    """Data coordinator for SSD IMS integration."""

    def __init__(
        self, hass: HomeAssistant, api_client: SsdImsApiClient, config: Dict[str, Any]
    ) -> None:
        """Initialize coordinator."""
        self.api_client = api_client
        self.config = config
        self.pods: Dict[str, PointOfDelivery] = {}  # pod_id -> PointOfDelivery

        scan_interval = timedelta(
            minutes=config.get("scan_interval", DEFAULT_SCAN_INTERVAL)
        )

        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=scan_interval)

    async def _async_update_data(self) -> Dict[str, Any]:
        """Fetch data from API."""
        try:
            _LOGGER.info("Starting data update for SSD IMS integration")

            # Get POD configuration - now using stable pod_ids instead of pod_texts
            pod_ids = self.config.get(CONF_POINT_OF_DELIVERY, DEFAULT_POINT_OF_DELIVERY)
            if not pod_ids:
                _LOGGER.info("No PODs configured, discovering available PODs")
                # Discover PODs if not configured
                await self._discover_pods()
                pod_ids = list(self.pods.keys())
                _LOGGER.info("Discovered %d PODs: %s", len(pod_ids), pod_ids)
            else:
                _LOGGER.info("Using configured PODs: %s", pod_ids)
                _LOGGER.debug("Available POD IDs: %s", list(self.pods.keys()))

                # Check if we need to discover PODs (e.g., if configured PODs are not
                # found)
                if not self.pods:
                    _LOGGER.info("No PODs discovered yet, discovering available PODs")
                    await self._discover_pods()

                # Check if any configured PODs are not found
                missing_pods = [pod for pod in pod_ids if pod not in self.pods]
                if missing_pods:
                    _LOGGER.warning("Some configured PODs not found: %s", missing_pods)
                    _LOGGER.debug("Available PODs: %s", list(self.pods.keys()))
                    _LOGGER.debug("Configured PODs: %s", pod_ids)

                    # Remove missing PODs from the list
                    pod_ids = [pod for pod in pod_ids if pod in self.pods]
                    _LOGGER.info("Using available PODs: %s", pod_ids)

            # Use Slovakia timezone for correct local time calculations
            sk_tz = ZoneInfo("Europe/Bratislava")
            now = datetime.now(sk_tz)
            
            _LOGGER.debug("Current time in Slovakia timezone: %s", now)

            # Fetch data for each POD
            all_pod_data = {}

            for pod_id in pod_ids:
                _LOGGER.debug("Processing POD: %s", pod_id)
                try:
                    pod = self.pods.get(pod_id)
                    if not pod:
                        _LOGGER.warning(
                            "POD %s not found in discovered PODs, skipping", pod_id
                        )
                        _LOGGER.debug("Available PODs: %s", list(self.pods.keys()))
                        continue

                    # Calculate date ranges for each period in Slovakia timezone
                    yesterday_start = now.replace(
                        hour=0, minute=0, second=0, microsecond=0
                    ) - timedelta(days=1)
                    yesterday_end = yesterday_start + timedelta(days=1)

                    # Last week: previous 7 days ending yesterday
                    last_week_start = yesterday_start - timedelta(days=6)
                    last_week_end = yesterday_end

                    _LOGGER.debug(
                        "Time periods for POD %s - yesterday: %s to %s, last_week: %s to %s",
                        pod_id,
                        yesterday_start,
                        yesterday_end,
                        last_week_start,
                        last_week_end,
                    )

                    # Fetch separate chart data for each period
                    chart_data_yesterday = await self.api_client.get_chart_data(
                        pod_id, yesterday_start, yesterday_end
                    )
                    
                    chart_data_last_week = await self.api_client.get_chart_data(
                        pod_id, last_week_start, last_week_end
                    )

                    _LOGGER.debug(
                        "Yesterday data for POD %s: metering_datetime count=%d, sum_actual_consumption=%s",
                        pod_id,
                        len(chart_data_yesterday.metering_datetime),
                        chart_data_yesterday.sum_actual_consumption,
                    )
                    
                    _LOGGER.debug(
                        "Last week data for POD %s: metering_datetime count=%d, sum_actual_consumption=%s",
                        pod_id,
                        len(chart_data_last_week.metering_datetime),
                        chart_data_last_week.sum_actual_consumption,
                    )

                    # Aggregate data by time periods using separate chart data
                    aggregated_data = self._aggregate_data(chart_data_yesterday, chart_data_last_week)

                    _LOGGER.debug(
                        "Aggregated data for POD %s: %s", pod_id, aggregated_data
                    )

                    # Store POD data using stable pod_id as key
                    all_pod_data[pod_id] = {
                        "session_pod_id": pod.value,  # Store session pod_id for internal
                        # reference
                        "pod_text": pod.text,  # Store original text for
                        # reference
                        "chart_data_yesterday": chart_data_yesterday,
                        "chart_data_last_week": chart_data_last_week,
                        "aggregated_data": aggregated_data,
                        "last_update": now.isoformat(),
                    }

                except Exception as e:
                    error_msg = str(e)
                    _LOGGER.error(
                        "Error fetching data for POD %s: %s", pod_id, error_msg
                    )

                    # Check if this is an authentication error
                    if any(
                        auth_error in error_msg.lower()
                        for auth_error in [
                            "not authenticated",
                            "authentication failed",
                            "session expired",
                            "re-authentication failed",
                            "text/html",
                        ]
                    ):
                        _LOGGER.error(
                            "Authentication error detected, stopping data update"
                        )
                        raise ConfigEntryAuthFailed("Authentication failed") from e

                    # Continue with other PODs for non-auth errors
                    continue

            # Note: We no longer track last_update time since we always fetch full 7-day range

            _LOGGER.info(
                "Data update completed successfully for %d PODs", len(all_pod_data)
            )
            return all_pod_data

        except ConfigEntryAuthFailed:
            # Re-raise authentication failures
            raise
        except Exception as e:
            _LOGGER.error("Error updating data: %s", e)
            if "Not authenticated" in str(e) or "Authentication failed" in str(e):
                raise ConfigEntryAuthFailed("Authentication failed") from e
            raise UpdateFailed(f"Error updating data: {e}") from e

    async def _discover_pods(self) -> None:
        """Discover points of delivery."""
        try:
            pods = await self.api_client.get_points_of_delivery()
            if not pods:
                raise Exception("No points of delivery found")

            _LOGGER.debug(
                "Raw PODs from API: %s", [(pod.text, pod.value) for pod in pods]
            )

            # Store PODs by stable pod.id instead of text for stable identification
            self.pods = {}
            for pod in pods:
                try:
                    pod_id = pod.id  # Use stable 16-20 char ID
                    self.pods[pod_id] = pod
                except ValueError as e:
                    _LOGGER.warning(
                        "Skipping POD with invalid ID format: %s - %s", pod.text, e
                    )
                    continue

            _LOGGER.info("Discovered %d PODs", len(self.pods))
            _LOGGER.debug(
                "POD mapping (id -> value): %s",
                {pod_id: pod.value for pod_id, pod in self.pods.items()},
            )

        except Exception as e:
            error_msg = str(e)
            _LOGGER.error("Error discovering PODs: %s", error_msg)

            # Check if this is an authentication error
            if any(
                auth_error in error_msg.lower()
                for auth_error in [
                    "not authenticated",
                    "authentication failed",
                    "session expired",
                    "re-authentication failed",
                    "text/html",
                ]
            ):
                raise ConfigEntryAuthFailed(
                    "Authentication failed during POD discovery"
                ) from e

            raise

    def _aggregate_data(self, chart_data_yesterday: ChartData, chart_data_last_week: ChartData) -> Dict[str, Dict[str, float]]:
        """Aggregate data by different time periods using separate chart data for each period."""
        # Initialize aggregated data structure
        aggregated = {"yesterday": {}, "last_week": {}}

        # Use yesterday chart data for yesterday period
        if chart_data_yesterday and hasattr(chart_data_yesterday, "sum_actual_consumption"):
            aggregated["yesterday"][SENSOR_TYPE_ACTUAL_CONSUMPTION] = (
                chart_data_yesterday.sum_actual_consumption or 0.0
            )
            aggregated["yesterday"][SENSOR_TYPE_ACTUAL_SUPPLY] = (
                chart_data_yesterday.sum_actual_supply or 0.0
            )
            aggregated["yesterday"][SENSOR_TYPE_IDLE_CONSUMPTION] = (
                chart_data_yesterday.sum_idle_consumption or 0.0
            )
            aggregated["yesterday"][SENSOR_TYPE_IDLE_SUPPLY] = (
                chart_data_yesterday.sum_idle_supply or 0.0
            )

            _LOGGER.debug(
                "Yesterday chart data summaries: actual_consumption=%s, "
                "actual_supply=%s, idle_consumption=%s, idle_supply=%s",
                chart_data_yesterday.sum_actual_consumption,
                chart_data_yesterday.sum_actual_supply,
                chart_data_yesterday.sum_idle_consumption,
                chart_data_yesterday.sum_idle_supply,
            )
        else:
            # Fallback: set all values to 0 if no chart data available
            for sensor_type in SENSOR_TYPES:
                aggregated["yesterday"][sensor_type] = 0.0
            _LOGGER.warning("No yesterday chart data available, setting yesterday values to 0")

        # Use last week chart data for last week period
        if chart_data_last_week and hasattr(chart_data_last_week, "sum_actual_consumption"):
            aggregated["last_week"][SENSOR_TYPE_ACTUAL_CONSUMPTION] = (
                chart_data_last_week.sum_actual_consumption or 0.0
            )
            aggregated["last_week"][SENSOR_TYPE_ACTUAL_SUPPLY] = (
                chart_data_last_week.sum_actual_supply or 0.0
            )
            aggregated["last_week"][SENSOR_TYPE_IDLE_CONSUMPTION] = (
                chart_data_last_week.sum_idle_consumption or 0.0
            )
            aggregated["last_week"][SENSOR_TYPE_IDLE_SUPPLY] = (
                chart_data_last_week.sum_idle_supply or 0.0
            )

            _LOGGER.debug(
                "Last week chart data summaries: actual_consumption=%s, "
                "actual_supply=%s, idle_consumption=%s, idle_supply=%s",
                chart_data_last_week.sum_actual_consumption,
                chart_data_last_week.sum_actual_supply,
                chart_data_last_week.sum_idle_consumption,
                chart_data_last_week.sum_idle_supply,
            )
        else:
            # Fallback: set all values to 0 if no chart data available
            for sensor_type in SENSOR_TYPES:
                aggregated["last_week"][sensor_type] = 0.0
            _LOGGER.warning("No last week chart data available, setting last week values to 0")

        return aggregated
