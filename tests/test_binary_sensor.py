"""Tests for the cloud-link binary sensor."""

from unittest.mock import MagicMock

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.const import EntityCategory

from custom_components.ssd_ims.binary_sensor import SsdImsCloudLinkSensor


def test_cloud_link_sensor_follows_flag_and_stays_available():
    coordinator = MagicMock()
    coordinator.cloud_link_ok = True
    entry = MagicMock()
    entry.entry_id = "abc"
    entry.title = "Home"

    sensor = SsdImsCloudLinkSensor(coordinator, entry)

    assert sensor.device_class is BinarySensorDeviceClass.CONNECTIVITY
    assert sensor.entity_category is EntityCategory.DIAGNOSTIC
    assert sensor.available is True
    assert sensor.is_on is True

    coordinator.cloud_link_ok = False
    assert sensor.available is True
    assert sensor.is_on is False
