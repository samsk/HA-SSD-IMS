"""Sensor platform for SSD IMS integration."""
import logging
import re
from typing import Any, Optional

from homeassistant.components.sensor import (SensorDeviceClass, SensorEntity,
                                             SensorStateClass)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType

from .const import (CONF_POD_NAME_MAPPING, CONF_POINT_OF_DELIVERY,
                    DEFAULT_POINT_OF_DELIVERY, DOMAIN,
                    SENSOR_TYPE_ACTUAL_CONSUMPTION, SENSOR_TYPE_ACTUAL_SUPPLY, 
                    SENSOR_TYPE_IDLE_CONSUMPTION, SENSOR_TYPE_IDLE_SUPPLY, 
                    SENSOR_TYPES, TIME_PERIODS, TIME_PERIODS_CONFIG)
from .coordinator import SsdImsDataCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SSD IMS sensors from config entry."""
    coordinator: SsdImsDataCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    # Get POD configuration - now using stable pod_ids instead of pod_texts
    pod_ids = config_entry.data.get(CONF_POINT_OF_DELIVERY, DEFAULT_POINT_OF_DELIVERY)
    pod_name_mapping = config_entry.data.get(CONF_POD_NAME_MAPPING, {})

    # Create sensors for each POD
    sensors = []
    for pod_id in pod_ids:
        # Get friendly name for this POD
        friendly_name = pod_name_mapping.get(pod_id)
        if not friendly_name:
            # Use POD ID as fallback
            friendly_name = pod_id

        # Sanitize friendly name for use in sensor names
        friendly_name = _sanitize_name(friendly_name)

        # Create sensors for all combinations of sensor types and time periods
        for sensor_type in SENSOR_TYPES:
            for period in TIME_PERIODS:
                sensors.append(
                    SsdImsSensor(
                        coordinator, sensor_type, period, pod_id, friendly_name
                    )
                )

    async_add_entities(sensors)


def _sanitize_name(name: str) -> str:
    """Sanitize name for use in sensor names."""
    # Replace spaces and special characters with underscores
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    # Remove multiple consecutive underscores
    sanitized = re.sub(r"_+", "_", sanitized)
    # Remove leading/trailing underscores
    sanitized = sanitized.strip("_")
    return sanitized


class SsdImsSensor(SensorEntity):
    """Base sensor for SSD IMS data."""

    def __init__(
        self,
        coordinator: SsdImsDataCoordinator,
        sensor_type: str,
        period: str,
        pod_id: str,  # Now using stable pod_id instead of pod_text
        friendly_name: str,
    ) -> None:
        """Initialize sensor."""
        self.coordinator = coordinator
        self.sensor_type = sensor_type
        self.period = period
        self.pod_id = pod_id  # Store stable pod_id for identification
        self.friendly_name = friendly_name

        # Generate sensor name (without POD info)
        sensor_name = self._generate_sensor_name()
        
        # Sanitize sensor name for unique ID
        sanitized_sensor_name = _sanitize_name(sensor_name)
        
        # Set unique ID - use pod_id + sensor_name format for entity ID
        self._attr_unique_id = f"{pod_id}_{sanitized_sensor_name}"

        # Setup entity naming using the new Home Assistant entity naming convention
        self._setup_entity_naming(friendly_name, sensor_name, pod_id)

        # Set unit attributes based on sensor type
        if self.sensor_type in [
            SENSOR_TYPE_ACTUAL_CONSUMPTION,
            SENSOR_TYPE_ACTUAL_SUPPLY,
        ]:
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING  # For energy sensors
        elif self.sensor_type in [
            SENSOR_TYPE_IDLE_CONSUMPTION,
            SENSOR_TYPE_IDLE_SUPPLY,
        ]:
            self._attr_native_unit_of_measurement = "kVARh"
            self._attr_device_class = None  # No device class for reactive power
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING  # For reactive energy
        else:
            self._attr_native_unit_of_measurement = None
            self._attr_device_class = None
            self._attr_state_class = None

        # Set device info
        self._attr_device_info = {
            "identifiers": {(DOMAIN, self.pod_id)},  # Use stable pod_id as identifier
            "name": self.friendly_name,  # Device name (POD friendly name)
            "manufacturer": "IMS.SSD.sk",
            "model": "IMS Portal",
            "sw_version": self.pod_id,  # Add POD ID as software version
        }

    @property
    def device_class(self) -> Optional[SensorDeviceClass]:
        """Return device class."""
        return self._attr_device_class

    @property
    def state_class(self) -> Optional[SensorStateClass]:
        """Return state class."""
        return self._attr_state_class

    @property
    def native_unit_of_measurement(self) -> Optional[str]:
        """Return native unit of measurement."""
        return self._attr_native_unit_of_measurement

    @property
    def unit_of_measurement(self) -> Optional[str]:
        """Return unit of measurement (legacy property for compatibility)."""
        return self._attr_native_unit_of_measurement

    @property
    def native_value(self) -> Optional[StateType]:
        """Return sensor value."""
        if not self.coordinator.data:
            _LOGGER.debug(
                "Sensor %s: No coordinator data available", self._attr_unique_id
            )
            return None

        # Get data for this specific POD using stable pod_id
        pod_data = self.coordinator.data.get(self.pod_id, {})
        if not pod_data:
            _LOGGER.debug(
                "Sensor %s: No data for POD %s", self._attr_unique_id, self.pod_id
            )
            return None

        aggregated_data = pod_data.get("aggregated_data", {})
        if not aggregated_data:
            _LOGGER.debug(
                "Sensor %s: No aggregated data for POD %s",
                self._attr_unique_id,
                self.pod_id,
            )
            return None

        period_data = aggregated_data.get(self.period, {})
        if not period_data:
            _LOGGER.debug(
                "Sensor %s: No data for period %s", self._attr_unique_id, self.period
            )
            return None

        value = period_data.get(self.sensor_type)
        _LOGGER.debug(
            "Sensor %s: Value=%s (type=%s, period=%s, sensor_type=%s)",
            self._attr_unique_id,
            value,
            type(value).__name__,
            self.period,
            self.sensor_type,
        )
        
        # Ensure value is numeric (float) for proper unit display
        if value is not None:
            try:
                return float(value)
            except (ValueError, TypeError):
                _LOGGER.warning(
                    "Sensor %s: Could not convert value %s to float", 
                    self._attr_unique_id, 
                    value
                )
                return None
        return None

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.last_update_success

    @property
    def should_poll(self) -> bool:
        """Return False as this entity is updated via coordinator."""
        return False

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )

    def _generate_sensor_name(self) -> str:
        """Generate sensor name based on type and period using configuration."""
        # Sensor type names  
        type_names = {
            SENSOR_TYPE_ACTUAL_CONSUMPTION: "Active Consumption",
            SENSOR_TYPE_ACTUAL_SUPPLY: "Active Supply",
            SENSOR_TYPE_IDLE_CONSUMPTION: "Idle Consumption",
            SENSOR_TYPE_IDLE_SUPPLY: "Idle Supply",
        }

        type_name = type_names.get(self.sensor_type, self.sensor_type)
        
        # Get period display name from configuration
        period_config = TIME_PERIODS_CONFIG.get(self.period, {})
        period_name = period_config.get("display_name", self.period)

        return f"{type_name} {period_name}"

    def _setup_entity_naming(self, device_name: str, sensor_name: str, pod_id: str) -> None:
        """
        Setup entity naming using the new Home Assistant entity naming convention.

        This method implements the new Home Assistant entity naming standard:
        - Sets has_entity_name = True
        - Stores the friendly name for the name property
        - The name property will return only the data point name
        - Home Assistant automatically generates friendly_name by combining entity name with device name

        Args:
            device_name: The device name (POD friendly name)
            sensor_name: The sensor/control name from API (data point name)
            pod_id: The POD ID for identification
        """
        # Set has_entity_name to True for new Home Assistant naming convention
        self._attr_has_entity_name = True
        self._attr_name = sensor_name

        # Store additional info for backward compatibility if needed
        self._device_name = device_name
        self._sensor_name = sensor_name
        self._pod_id_stored = pod_id

    @property
    def extra_state_attributes(self) -> Optional[dict[str, Any]]:
        """Return extra state attributes."""
        if not self.coordinator.data:
            return None

        attrs = {
            "sensor_type": self.sensor_type,
            "time_period": self.period,
            "pod_id": self.pod_id,  # Use stable pod_id
            "friendly_name": self.friendly_name,
        }

        # Add POD information if available
        pod_data = self.coordinator.data.get(self.pod_id, {})
        if pod_data:
            attrs["pod_text"] = pod_data.get("pod_text")  # Original text for reference

        return attrs
