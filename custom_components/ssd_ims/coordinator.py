"""Data coordinator for SSD IMS integration."""
import asyncio
import logging
import random
from datetime import datetime, timedelta
from typing import Any, Dict
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (DataUpdateCoordinator,
                                                      UpdateFailed)

from .api_client import SsdImsApiClient
from .const import (API_DELAY_MAX, API_DELAY_MIN, CONF_ENABLE_IDLE_SENSORS,
                    CONF_ENABLE_SUPPLY_SENSORS, CONF_POINT_OF_DELIVERY,
                    CONF_SCAN_INTERVAL, DEFAULT_ENABLE_IDLE_SENSORS,
                    DEFAULT_ENABLE_SUPPLY_SENSORS, DEFAULT_POINT_OF_DELIVERY,
                    DEFAULT_SCAN_INTERVAL, DOMAIN, SENSOR_TYPE_ACTUAL_CONSUMPTION,
                    SENSOR_TYPE_ACTUAL_SUPPLY, SENSOR_TYPE_IDLE_CONSUMPTION,
                    SENSOR_TYPE_IDLE_SUPPLY, TIME_PERIODS_CONFIG)
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

    async def update_config(self, new_config: Dict[str, Any]) -> None:
        """Update coordinator configuration and trigger data refresh."""
        old_config = self.config.copy()
        self.config.update(new_config)
        
        # Check if sensor configuration changed
        sensor_config_changed = (
            old_config.get(CONF_ENABLE_SUPPLY_SENSORS) != new_config.get(CONF_ENABLE_SUPPLY_SENSORS) or
            old_config.get(CONF_ENABLE_IDLE_SENSORS) != new_config.get(CONF_ENABLE_IDLE_SENSORS)
        )
        
        # Check if scan interval changed
        scan_interval_changed = (
            old_config.get(CONF_SCAN_INTERVAL) != new_config.get(CONF_SCAN_INTERVAL)
        )
        
        # Update scan interval if changed
        if scan_interval_changed:
            new_scan_interval = new_config.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            self.update_interval = timedelta(minutes=new_scan_interval)
            _LOGGER.info("Updated scan interval to %d minutes", new_scan_interval)
        
        # Trigger immediate data refresh if sensor configuration changed
        if sensor_config_changed:
            _LOGGER.info(
                "Sensor configuration changed (supply: %s, idle: %s), triggering immediate data refresh",
                new_config.get(CONF_ENABLE_SUPPLY_SENSORS),
                new_config.get(CONF_ENABLE_IDLE_SENSORS)
            )
            await self.async_request_refresh()
        else:
            _LOGGER.debug("No sensor configuration changes detected")

    def _get_random_api_delay(self) -> float:
        """Get random API delay between configured min and max values."""
        # Generate random delay between API_DELAY_MIN and API_DELAY_MAX
        delay = random.uniform(API_DELAY_MIN, API_DELAY_MAX)
        # Ensure minimum of 1 second (hardcoded as requested)
        return max(0.3, delay)

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

            for pod_index, pod_id in enumerate(pod_ids):
                _LOGGER.debug("Processing POD: %s", pod_id)
                try:
                    pod = self.pods.get(pod_id)
                    if not pod:
                        _LOGGER.warning(
                            "POD %s not found in discovered PODs, skipping", pod_id
                        )
                        _LOGGER.debug("Available PODs: %s", list(self.pods.keys()))
                        continue

                    # Calculate date ranges and fetch data for each configured period
                    chart_data_by_period = {}
                    period_info = {}
                    
                    _LOGGER.debug("Using random API delay between %s-%s seconds between requests", API_DELAY_MIN, API_DELAY_MAX)
                    
                    for period_index, (period_key, config) in enumerate(TIME_PERIODS_CONFIG.items()):
                        try:
                            # Use the callback to calculate date range
                            calculate_range = config["calculate_range"]
                            period_start, period_end = calculate_range(now)
                            
                            period_info[period_key] = {
                                "start": period_start,
                                "end": period_end,
                                "days": (period_end - period_start).days,
                                "display_name": config["display_name"]
                            }
                            
                            # Add delay before API call (except for first request)
                            if period_index > 0:
                                delay = self._get_random_api_delay()
                                _LOGGER.debug(
                                    "Sleeping %.2f seconds before fetching %s data for POD %s",
                                    delay, period_key, pod_id
                                )
                                await asyncio.sleep(delay)
                            
                            # Fetch chart data for this period
                            _LOGGER.debug("Fetching %s data for POD %s", period_key, pod_id)
                            chart_data_by_period[period_key] = await self.api_client.get_chart_data(
                                pod_id, period_start, period_end
                            )
                            
                        except Exception as e:
                            _LOGGER.error(
                                "Error calculating date range for period %s: %s", 
                                period_key, e
                            )
                            continue

                    _LOGGER.debug(
                        "Time periods for POD %s: %s",
                        pod_id,
                        {k: f"{v['start']} to {v['end']} ({v['days']} days)" 
                         for k, v in period_info.items()}
                    )

                    # Log chart data summary for debugging
                    for period_key, chart_data in chart_data_by_period.items():
                        _LOGGER.debug(
                            "%s data for POD %s: metering_datetime count=%d, sum_actual_consumption=%s",
                            TIME_PERIODS_CONFIG[period_key]["display_name"],
                            pod_id,
                            len(chart_data.metering_datetime),
                            chart_data.sum_actual_consumption,
                        )

                    # Aggregate data by time periods using configurable chart data
                    aggregated_data = self._aggregate_data(chart_data_by_period)

                    _LOGGER.debug(
                        "Aggregated data for POD %s: %s", pod_id, aggregated_data
                    )

                    # Store POD data using stable pod_id as key
                    pod_data = {
                        "session_pod_id": pod.value,  # Store session pod_id for internal reference
                        "pod_text": pod.text,  # Store original text for reference
                        "aggregated_data": aggregated_data,
                        "last_update": now.isoformat(),
                    }
                    
                    # Add chart data for each period dynamically
                    for period_key, chart_data in chart_data_by_period.items():
                        pod_data[f"chart_data_{period_key}"] = chart_data
                    
                    all_pod_data[pod_id] = pod_data

                    # Add delay between PODs (except for last POD)
                    if pod_index < len(pod_ids) - 1:
                        delay = self._get_random_api_delay()
                        _LOGGER.debug(
                            "Sleeping %.2f seconds before processing next POD",
                            delay
                        )
                        await asyncio.sleep(delay)

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

    def _aggregate_data(self, chart_data_by_period: Dict[str, ChartData]) -> Dict[str, Dict[str, float]]:
        """Aggregate data by different time periods using configurable chart data."""
        aggregated = {}
        
        # Get sensor configuration options
        enable_supply_sensors = self.config.get(
            CONF_ENABLE_SUPPLY_SENSORS, DEFAULT_ENABLE_SUPPLY_SENSORS
        )
        enable_idle_sensors = self.config.get(
            CONF_ENABLE_IDLE_SENSORS, DEFAULT_ENABLE_IDLE_SENSORS
        )

        # Create list of enabled sensor types
        enabled_sensor_types = [SENSOR_TYPE_ACTUAL_CONSUMPTION]  # Always enabled
        
        if enable_supply_sensors:
            enabled_sensor_types.append(SENSOR_TYPE_ACTUAL_SUPPLY)
        
        if enable_idle_sensors:
            enabled_sensor_types.extend([
                SENSOR_TYPE_IDLE_CONSUMPTION,
                SENSOR_TYPE_IDLE_SUPPLY,
            ])
        
        for period_key, chart_data in chart_data_by_period.items():
            period_name = TIME_PERIODS_CONFIG[period_key]["display_name"]
            aggregated[period_key] = {}
            
            if chart_data and hasattr(chart_data, "sum_actual_consumption"):
                # Extract only enabled sensor values for this period
                if SENSOR_TYPE_ACTUAL_CONSUMPTION in enabled_sensor_types:
                    aggregated[period_key][SENSOR_TYPE_ACTUAL_CONSUMPTION] = (
                        chart_data.sum_actual_consumption or 0.0
                    )
                
                if SENSOR_TYPE_ACTUAL_SUPPLY in enabled_sensor_types:
                    aggregated[period_key][SENSOR_TYPE_ACTUAL_SUPPLY] = (
                        chart_data.sum_actual_supply or 0.0
                    )
                
                if SENSOR_TYPE_IDLE_CONSUMPTION in enabled_sensor_types:
                    aggregated[period_key][SENSOR_TYPE_IDLE_CONSUMPTION] = (
                        chart_data.sum_idle_consumption or 0.0
                    )
                
                if SENSOR_TYPE_IDLE_SUPPLY in enabled_sensor_types:
                    aggregated[period_key][SENSOR_TYPE_IDLE_SUPPLY] = (
                        chart_data.sum_idle_supply or 0.0
                    )

                # Debug log only for enabled sensors
                debug_msg = f"{period_name} chart data summaries: actual_consumption={chart_data.sum_actual_consumption}"
                if enable_supply_sensors:
                    debug_msg += f", actual_supply={chart_data.sum_actual_supply}"
                if enable_idle_sensors:
                    debug_msg += f", idle_consumption={chart_data.sum_idle_consumption}, idle_supply={chart_data.sum_idle_supply}"
                
                _LOGGER.debug(debug_msg)
            else:
                # Fallback: set enabled sensor values to 0 if no chart data available
                for sensor_type in enabled_sensor_types:
                    aggregated[period_key][sensor_type] = 0.0
                _LOGGER.warning("No %s chart data available, setting %s values to 0", period_name, period_key)

        return aggregated
