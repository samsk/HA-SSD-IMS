"""Tests for the cloud-link binary sensor."""

from unittest.mock import MagicMock

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.const import EntityCategory

from custom_components.ssd_ims.binary_sensor import SsdImsCloudLinkSensor
from custom_components.ssd_ims.const import DOMAIN


def test_cloud_link_sensor_uses_pod_device():
    coordinator = MagicMock()
    coordinator.cloud_link_ok = True
    entry = MagicMock()
    entry.entry_id = "abc"
    entry.title = "Home"
    pod_id = "99XXX1234560000G"

    sensor = SsdImsCloudLinkSensor(coordinator, entry, pod_id, "H1")

    assert sensor.device_class is BinarySensorDeviceClass.CONNECTIVITY
    assert sensor.entity_category is EntityCategory.DIAGNOSTIC
    assert sensor.device_info["identifiers"] == {(DOMAIN, pod_id)}
    assert sensor.device_info["name"] == "H1"
    assert sensor.available is True
    assert sensor.is_on is True

    coordinator.cloud_link_ok = False
    assert sensor.available is True
    assert sensor.is_on is False
