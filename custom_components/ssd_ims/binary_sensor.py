"""Binary sensors for SSD IMS integration."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, PORTAL_URL
from .coordinator import SsdImsDataCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the cloud-link binary sensor for a config entry."""
    coordinator: SsdImsDataCoordinator = config_entry.runtime_data
    async_add_entities([SsdImsCloudLinkSensor(coordinator, config_entry)])


class SsdImsCloudLinkSensor(BinarySensorEntity):
    """On when the portal last accepted login; always available."""

    _attr_has_entity_name = True
    _attr_translation_key = "cloud_link"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: SsdImsDataCoordinator, entry: ConfigEntry) -> None:
        """Bind to the entry-scoped coordinator."""
        self.coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_cloud_link"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Stredoslovenská distribučná, a.s.",
            model="SSD IMS",
            configuration_url=PORTAL_URL,
        )

    @property
    def available(self) -> bool:
        """Stay available while the cloud link is down."""
        return True

    @property
    def is_on(self) -> bool:
        """Return True when the portal accepted the last login."""
        return self.coordinator.cloud_link_ok

    async def async_added_to_hass(self) -> None:
        """Subscribe to coordinator listener updates."""
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )
