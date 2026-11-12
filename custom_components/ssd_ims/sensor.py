"""Sensor platform for SSD IMS integration."""

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    CONF_POD_NAME_MAPPING,
    CONF_POINT_OF_DELIVERY,
    CONF_UNPUBLISHED_AS_UNKNOWN,
    DEFAULT_POINT_OF_DELIVERY,
    DOMAIN,
    PORTAL_URL,
    SENSOR_TYPE_ACTUAL_CONSUMPTION,
    SENSOR_TYPE_ACTUAL_SUPPLY,
    SENSOR_TYPE_LABELS,
)
from .coordinator import SsdImsDataCoordinator
from .helpers import sanitize_name

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SSD IMS sensors from config entry."""
    coordinator: SsdImsDataCoordinator = config_entry.runtime_data

    pod_ids = config_entry.data.get(CONF_POINT_OF_DELIVERY, DEFAULT_POINT_OF_DELIVERY)
    pod_name_mapping = config_entry.data.get(CONF_POD_NAME_MAPPING, {})

    enabled_sensor_types = [SENSOR_TYPE_ACTUAL_CONSUMPTION, SENSOR_TYPE_ACTUAL_SUPPLY]

    sensors: list[SensorEntity] = []
    for pod_id in pod_ids:
        friendly_name = pod_name_mapping.get(pod_id, pod_id)
        sensors.append(SsdImsLastUpdateSensor(coordinator, pod_id, friendly_name))
        sensors.extend(
            SsdImsRegisterSensor(coordinator, pod_id, friendly_name, description)
            for description in REGISTER_SENSORS
        )
        sensors.extend(
            SsdImsTrendSensor(coordinator, pod_id, friendly_name, description)
            for description in TREND_SENSORS
        )
        for sensor_type in enabled_sensor_types:
            sensors.append(
                SsdImsYesterdaySensor(
                    coordinator,
                    sensor_type,
                    pod_id,
                    friendly_name,
                )
            )
            sensors.append(
                SsdImsCumulativeSensor(
                    coordinator,
                    sensor_type,
                    pod_id,
                    friendly_name,
                )
            )

    async_add_entities(sensors)


class SsdImsSensor(CoordinatorEntity[SsdImsDataCoordinator], SensorEntity):
    """Base sensor for SSD IMS data."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SsdImsDataCoordinator,
        pod_id: str,
        friendly_name: str,
    ) -> None:
        """Initialize sensor."""
        super().__init__(coordinator)
        self.pod_id = pod_id
        self.friendly_name = friendly_name

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self.pod_id)},
            name=self.friendly_name,
            manufacturer="Stredoslovenská distribučná, a.s.",
            model="Odberné miesto elektriny",
            configuration_url=PORTAL_URL,
            # The EIC code from the invoice, so the device can be matched to it.
            serial_number=self.pod_id,
        )

    def _setup_entity_naming(self, sensor_name: str) -> None:
        """Set up entity naming."""
        self._attr_name = sensor_name


class SsdImsEnergySensor(SsdImsSensor):
    """Base class for energy sensors."""

    def __init__(
        self,
        coordinator: SsdImsDataCoordinator,
        sensor_type: str,
        pod_id: str,
        friendly_name: str,
    ) -> None:
        """Initialize energy sensor."""
        super().__init__(coordinator, pod_id, friendly_name)
        self.sensor_type = sensor_type
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_device_class = SensorDeviceClass.ENERGY

    def _generate_sensor_name(self, suffix: str) -> str:
        """Generate sensor name based on type and suffix."""
        type_name = SENSOR_TYPE_LABELS.get(self.sensor_type, "Energy")
        return f"{type_name} {suffix}"


class SsdImsYesterdaySensor(SsdImsEnergySensor):
    """Sensor for yesterday's values."""

    _attr_entity_registry_enabled_default = True

    def __init__(
        self,
        coordinator: SsdImsDataCoordinator,
        sensor_type: str,
        pod_id: str,
        friendly_name: str,
    ) -> None:
        """Initialize yesterday sensor."""
        super().__init__(coordinator, sensor_type, pod_id, friendly_name)
        # This is a self-contained daily snapshot, replaced once a day, and
        # can legitimately be lower than the previous day's value — not a
        # running total. TOTAL_INCREASING/TOTAL would make HA generate its
        # own long-term statistics for this entity, duplicating (and
        # potentially conflicting with) the external statistics the
        # coordinator writes directly for the Energy dashboard. MEASUREMENT
        # isn't a legal alternative either: HA's ENERGY device class only
        # accepts state_class None, TOTAL, or TOTAL_INCREASING (using
        # MEASUREMENT logs an "impossible" warning and is expected to raise
        # in a future HA version) — so leave state_class unset (None), which
        # also means no HA-generated statistics at all for this entity.

        sensor_name = self._generate_sensor_name("Yesterday")
        sanitized_sensor_name = sanitize_name(sensor_name)
        self._attr_unique_id = f"{pod_id}_{sanitized_sensor_name}_{sensor_type}"
        self._setup_entity_naming(sensor_name)

    @property
    def native_value(self) -> StateType | None:
        """Return sensor value (yesterday's total)."""
        if self.coordinator.config.get(CONF_UNPUBLISHED_AS_UNKNOWN) and not getattr(
            self.coordinator, "yesterday_complete", False
        ):
            return None
        if not self.coordinator.data or not (
            pod_data := self.coordinator.data.get(self.pod_id)
        ):
            return None

        aggregated_data = pod_data.get("aggregated_data", {})
        value = aggregated_data.get(self.sensor_type)
        return float(value) if value is not None else None


class SsdImsCumulativeSensor(SsdImsEnergySensor):
    """Sensor for cumulative total energy imported from statistics."""

    def __init__(
        self,
        coordinator: SsdImsDataCoordinator,
        sensor_type: str,
        pod_id: str,
        friendly_name: str,
    ) -> None:
        """Initialize cumulative sensor."""
        super().__init__(coordinator, sensor_type, pod_id, friendly_name)
        # This one genuinely mirrors an ever-increasing running total read
        # back from the coordinator's own statistics.
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING

        sensor_name = self._generate_sensor_name("Total")
        sanitized_sensor_name = sanitize_name(sensor_name)
        self._attr_unique_id = f"{pod_id}_{sanitized_sensor_name}_{sensor_type}_total"
        self._setup_entity_naming(sensor_name)

    @property
    def native_value(self) -> StateType | None:
        """Return cumulative total from statistics."""
        if not self.coordinator.data or not (
            pod_data := self.coordinator.data.get(self.pod_id)
        ):
            return None

        cumulative_totals = pod_data.get("cumulative_totals", {})
        value = cumulative_totals.get(self.sensor_type)
        return float(value) if value is not None else None


class SsdImsLastUpdateSensor(SsdImsSensor):
    def __init__(
        self,
        coordinator: SsdImsDataCoordinator,
        pod_id: str,
        friendly_name: str,
    ) -> None:
        """Initialize last update sensor."""
        super().__init__(coordinator, pod_id, friendly_name)
        self._attr_device_class = SensorDeviceClass.TIMESTAMP

        sensor_name = "Last Update"
        sanitized_sensor_name = sanitize_name(sensor_name)
        self._attr_unique_id = f"{pod_id}_{sanitized_sensor_name}"
        self._setup_entity_naming(sensor_name)

    @property
    def native_value(self) -> datetime | None:
        """Return sensor value (last update timestamp)."""
        if not self.coordinator.data or not (
            pod_data := self.coordinator.data.get(self.pod_id)
        ):
            return None

        last_update_str = pod_data.get("last_update")
        return dt_util.parse_datetime(last_update_str) if last_update_str else None


# (registry data key, entity name, unique-id suffix, is_energy)
# Demand sensors use translation keys + watts; names are English fallbacks.
REGISTER_SENSORS: tuple[tuple[str, str, str, bool], ...] = (
    ("consumption_vt", "High Tariff Meter Reading", "meter_reading_vt", True),
    ("consumption_nt", "Low Tariff Meter Reading", "meter_reading_nt", True),
    ("max_demand", "Peak Demand", "peak_demand", False),
    ("min_demand", "Base Demand", "low_demand", False),
)

# Register demand is stored as portal kW; sensors expose watts.
_REGISTER_DEMAND_WATT_SUFFIXES = frozenset({"peak_demand", "low_demand"})
_KW_TO_W = 1000.0

# (trends key, translation_key, unique-id suffix, unit, device_class, as_int)
TREND_SENSORS: tuple[
    tuple[str, str, str, str | None, SensorDeviceClass | None, bool],
    ...,
] = (
    (
        "low_demand_kw",
        "base_demand_yesterday",
        "low_demand_yesterday",
        UnitOfPower.WATT,
        SensorDeviceClass.POWER,
        False,
    ),
    (
        "load_factor_pct",
        "load_factor_yesterday",
        "load_factor_yesterday",
        PERCENTAGE,
        None,
        False,
    ),
    (
        "peak_hour",
        "peak_hour_yesterday",
        "peak_hour_yesterday",
        None,
        None,
        True,
    ),
    (
        "wow_delta_kwh",
        "wow_delta",
        "wow_delta",
        UnitOfEnergy.KILO_WATT_HOUR,
        SensorDeviceClass.ENERGY,
        False,
    ),
    (
        "avg_7d_kwh",
        "avg_consumption_7d",
        "avg_consumption_7d",
        f"{UnitOfEnergy.KILO_WATT_HOUR}/d",
        None,
        False,
    ),
    (
        "avg_30d_kwh",
        "avg_consumption_30d",
        "avg_consumption_30d",
        f"{UnitOfEnergy.KILO_WATT_HOUR}/d",
        None,
        False,
    ),
    (
        "export_share_pct",
        "export_share_yesterday",
        "export_share_yesterday",
        PERCENTAGE,
        None,
        False,
    ),
)


class SsdImsRegisterSensor(SsdImsSensor):
    """Latest monthly meter register reading (Registrové údaje).

    The tariff readings are the meter's cumulative VT (1.8.1) / NT (1.8.2)
    counters; peak demand (1.6.0) is the highest quarter-hour demand of the
    period that ended at the reading. Values change at most a few times a
    month. For the Energy dashboard use the imported
    ``ssd_ims:<pod>_consumption_vt/_nt`` statistics instead, which place each
    month's consumption in the right month.
    """

    def __init__(
        self,
        coordinator: SsdImsDataCoordinator,
        pod_id: str,
        friendly_name: str,
        description: tuple[str, str, str, bool],
    ) -> None:
        """Initialize register sensor."""
        super().__init__(coordinator, pod_id, friendly_name)
        self._data_key, sensor_name, unique_suffix, is_energy = description
        self._demand_as_watt = unique_suffix in _REGISTER_DEMAND_WATT_SUFFIXES
        if is_energy:
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING
            self._setup_entity_naming(sensor_name)
        elif self._demand_as_watt:
            self._attr_device_class = SensorDeviceClass.POWER
            self._attr_native_unit_of_measurement = UnitOfPower.WATT
            self._attr_state_class = SensorStateClass.MEASUREMENT
            # peak_demand / base_demand — SK: Najvyšší / Najnižší odber
            self._attr_translation_key = (
                "base_demand" if unique_suffix == "low_demand" else "peak_demand"
            )
        else:
            self._attr_device_class = SensorDeviceClass.POWER
            self._attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._setup_entity_naming(sensor_name)
        self._attr_unique_id = f"{pod_id}_{unique_suffix}"
        # Portal often omits actualConsumptionMin; keep the entity off until
        # the user enables it (unlike Peak Demand / meter readings).
        if unique_suffix == "low_demand":
            self._attr_entity_registry_enabled_default = False

    def _registry(self) -> dict[str, Any] | None:
        if not self.coordinator.data or not (
            pod_data := self.coordinator.data.get(self.pod_id)
        ):
            return None
        return pod_data.get("registry")

    @property
    def native_value(self) -> StateType | None:
        """Return the latest register value (demand as watts)."""
        if not (registry := self._registry()):
            return None
        value = registry.get(self._data_key)
        if value is None:
            return None
        number = float(value)
        if self._demand_as_watt:
            return number * _KW_TO_W
        return number

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return reading date and meter serial number."""
        if not (registry := self._registry()):
            return None
        return {
            "reading_time": registry.get("reading_time"),
            "meter_serial_number": registry.get("serial_number"),
        }


class SsdImsTrendSensor(SsdImsSensor):
    """Optional virtual trend from yesterday chart or HA statistics."""

    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: SsdImsDataCoordinator,
        pod_id: str,
        friendly_name: str,
        description: tuple[str, str, str, str | None, SensorDeviceClass | None, bool],
    ) -> None:
        """Initialize a default-disabled trend sensor."""
        super().__init__(coordinator, pod_id, friendly_name)
        (
            self._data_key,
            translation_key,
            unique_suffix,
            unit,
            device_class,
            self._as_int,
        ) = description
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{pod_id}_{unique_suffix}"
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        if self._data_key in ("avg_7d_kwh", "avg_30d_kwh"):
            self._attr_suggested_display_precision = 3
        if device_class == SensorDeviceClass.POWER:
            self._attr_state_class = SensorStateClass.MEASUREMENT
        elif (
            device_class == SensorDeviceClass.ENERGY
            and self._data_key == "wow_delta_kwh"
        ):
            # Signed daily delta — not a running total.
            self._attr_state_class = None
        elif unit == PERCENTAGE:
            self._attr_state_class = SensorStateClass.MEASUREMENT

    def _trends(self) -> dict[str, Any] | None:
        if not self.coordinator.data or not (
            pod_data := self.coordinator.data.get(self.pod_id)
        ):
            return None
        return pod_data.get("trends")

    @property
    def native_value(self) -> StateType | None:
        """Return the trend value, or None when unknown."""
        # Yesterday-derived trends follow the unpublished-as-unknown option.
        chart_keys = {
            "low_demand_kw",
            "load_factor_pct",
            "peak_hour",
            "export_share_pct",
        }
        if (
            self._data_key in chart_keys
            and self.coordinator.config.get(CONF_UNPUBLISHED_AS_UNKNOWN)
            and not getattr(self.coordinator, "yesterday_complete", False)
        ):
            return None
        if not (trends := self._trends()):
            return None
        value = trends.get(self._data_key)
        if value is None:
            return None
        if self._as_int:
            return int(value)
        number = float(value)
        if self._data_key in ("avg_7d_kwh", "avg_30d_kwh"):
            return round(number, 3)
        # Chart min demand is stored as kW; expose watts without extra rounding.
        if self._data_key == "low_demand_kw":
            return number * _KW_TO_W
        return number

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Attach WoW percent on the week-over-week delta sensor."""
        if self._data_key != "wow_delta_kwh":
            return None
        if not (trends := self._trends()):
            return None
        percent = trends.get("wow_percent")
        if percent is None:
            return None
        return {"wow_percent": round(float(percent), 2)}
