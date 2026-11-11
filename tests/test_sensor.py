"""Tests for the SSD IMS sensor entities."""

from unittest.mock import MagicMock

from custom_components.ssd_ims.const import DOMAIN
from custom_components.ssd_ims.sensor import (
    REGISTER_SENSORS,
    TREND_SENSORS,
    SsdImsRegisterSensor,
    SsdImsSensor,
    SsdImsTrendSensor,
)


def test_device_info():
    """The device names the distributor, the EIC code and the portal."""
    sensor = SsdImsSensor(MagicMock(), "24ZSS0000000000X", "Rodinný dom")

    device_info = sensor.device_info
    assert device_info["identifiers"] == {(DOMAIN, "24ZSS0000000000X")}
    assert device_info["name"] == "Rodinný dom"
    assert device_info["serial_number"] == "24ZSS0000000000X"
    assert device_info["manufacturer"] == "Stredoslovenská distribučná, a.s."
    assert device_info["model"] == "Odberné miesto elektriny"
    assert device_info["configuration_url"] == "https://ims.ssd.sk"


def test_register_includes_low_demand():
    assert ("min_demand", "Low Demand", "low_demand", False) in REGISTER_SENSORS


def test_low_demand_register_unknown_when_missing():
    coordinator = MagicMock()
    coordinator.data = {
        "pod1": {
            "registry": {
                "max_demand": 3.0,
                "min_demand": None,
                "reading_time": "2026-10-31T23:00:00+00:00",
                "serial_number": "M1",
            }
        }
    }
    description = ("min_demand", "Low Demand", "low_demand", False)
    sensor = SsdImsRegisterSensor(coordinator, "pod1", "Home", description)
    assert sensor.native_value is None


def test_low_demand_register_value():
    coordinator = MagicMock()
    coordinator.data = {"pod1": {"registry": {"min_demand": 0.12}}}
    sensor = SsdImsRegisterSensor(
        coordinator, "pod1", "Home", ("min_demand", "Low Demand", "low_demand", False)
    )
    assert sensor.native_value == 0.12


def test_trend_sensors_disabled_by_default():
    coordinator = MagicMock()
    coordinator.data = {"pod1": {"trends": {"low_demand_kw": 0.5, "peak_hour": 14}}}
    for description in TREND_SENSORS:
        sensor = SsdImsTrendSensor(coordinator, "pod1", "Home", description)
        assert sensor.entity_registry_enabled_default is False
        assert sensor.unique_id.startswith("pod1_")


def test_trend_sensor_reads_trends_and_wow_attribute():
    coordinator = MagicMock()
    coordinator.data = {
        "pod1": {
            "trends": {
                "wow_delta_kwh": -2.5,
                "wow_percent": -10.0,
                "low_demand_kw": 0.0,
            }
        }
    }
    wow = next(d for d in TREND_SENSORS if d[0] == "wow_delta_kwh")
    sensor = SsdImsTrendSensor(coordinator, "pod1", "Home", wow)
    assert sensor.native_value == -2.5
    assert sensor.extra_state_attributes == {"wow_percent": -10.0}

    low = next(d for d in TREND_SENSORS if d[0] == "low_demand_kw")
    low_sensor = SsdImsTrendSensor(coordinator, "pod1", "Home", low)
    assert low_sensor.native_value == 0.0
    assert low_sensor.extra_state_attributes is None
