"""Tests for the SSD IMS sensor entities."""

from unittest.mock import MagicMock

from custom_components.ssd_ims.const import CONF_UNPUBLISHED_AS_UNKNOWN, DOMAIN
from custom_components.ssd_ims.sensor import (
    REGISTER_SENSORS,
    TREND_SENSORS,
    SsdImsRegisterSensor,
    SsdImsSensor,
    SsdImsTrendSensor,
    SsdImsYesterdaySensor,
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


def test_register_includes_base_demand():
    assert ("min_demand", "Base Demand", "low_demand", False) in REGISTER_SENSORS


def test_low_demand_register_disabled_by_default():
    """Portal often has no actualConsumptionMin — do not show Unknown by default."""
    sensor = SsdImsRegisterSensor(
        MagicMock(), "pod1", "Home", ("min_demand", "Base Demand", "low_demand", False)
    )
    assert sensor.entity_registry_enabled_default is False
    assert sensor.translation_key == "base_demand"
    assert sensor.native_unit_of_measurement == "W"


def test_peak_demand_register_enabled_by_default():
    sensor = SsdImsRegisterSensor(
        MagicMock(), "pod1", "Home", ("max_demand", "Peak Demand", "peak_demand", False)
    )
    assert sensor.entity_registry_enabled_default is True
    assert sensor.translation_key == "peak_demand"
    assert sensor.native_unit_of_measurement == "W"


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
    description = ("min_demand", "Base Demand", "low_demand", False)
    sensor = SsdImsRegisterSensor(coordinator, "pod1", "Home", description)
    assert sensor.native_value is None


def test_demand_register_values_as_watts():
    coordinator = MagicMock()
    coordinator.data = {"pod1": {"registry": {"min_demand": 0.12, "max_demand": 3.244}}}
    base = SsdImsRegisterSensor(
        coordinator, "pod1", "Home", ("min_demand", "Base Demand", "low_demand", False)
    )
    peak = SsdImsRegisterSensor(
        coordinator, "pod1", "Home", ("max_demand", "Peak Demand", "peak_demand", False)
    )
    assert base.native_value == 120.0
    assert peak.native_value == 3244.0


def test_trend_sensors_disabled_by_default():
    coordinator = MagicMock()
    coordinator.data = {"pod1": {"trends": {"low_demand_kw": 0.5, "peak_hour": 14}}}
    for description in TREND_SENSORS:
        sensor = SsdImsTrendSensor(coordinator, "pod1", "Home", description)
        assert sensor.entity_registry_enabled_default is False
        assert sensor.unique_id.startswith("pod1_")


def test_trend_sensor_reads_trends_and_wow_attribute():
    coordinator = MagicMock()
    coordinator.config = {}
    coordinator.yesterday_complete = True
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
    assert low_sensor.native_unit_of_measurement == "W"
    assert low_sensor.translation_key == "base_demand_yesterday"
    assert low_sensor.extra_state_attributes is None

    coordinator.data = {"pod1": {"trends": {"low_demand_kw": 0.52}}}
    assert low_sensor.native_value == 520.0


def test_float_trend_sensors_round_to_three_decimals():
    coordinator = MagicMock()
    coordinator.config = {}
    coordinator.yesterday_complete = True
    coordinator.data = {
        "pod1": {
            "trends": {
                "avg_7d_kwh": 73.5952857142859,
                "avg_30d_kwh": 50.1091666666666,
                "load_factor_pct": 46.8506384408602,
                "export_share_pct": 0.0017930144158359,
                "wow_delta_kwh": -2.5000001,
                "wow_percent": -10.0055555,
            }
        }
    }
    cases = (
        ("avg_7d_kwh", 73.595),
        ("avg_30d_kwh", 50.109),
        ("load_factor_pct", 46.851),
        ("export_share_pct", 0.002),
        ("wow_delta_kwh", -2.5),
    )
    for key, expected in cases:
        description = next(d for d in TREND_SENSORS if d[0] == key)
        sensor = SsdImsTrendSensor(coordinator, "pod1", "Home", description)
        assert sensor.native_value == expected
        assert sensor.suggested_display_precision == 3

    wow = next(d for d in TREND_SENSORS if d[0] == "wow_delta_kwh")
    assert SsdImsTrendSensor(
        coordinator, "pod1", "Home", wow
    ).extra_state_attributes == {"wow_percent": -10.006}


def test_yesterday_shows_zero_by_default_when_unpublished():
    coordinator = MagicMock()
    coordinator.config = {}
    coordinator.yesterday_complete = False
    coordinator.data = {
        "pod1": {"aggregated_data": {"actual_consumption": 0.0, "actual_supply": 0.0}}
    }
    sensor = SsdImsYesterdaySensor(coordinator, "actual_consumption", "pod1", "Home")
    assert sensor.native_value == 0.0


def test_yesterday_unknown_when_option_on_and_incomplete():
    coordinator = MagicMock()
    coordinator.config = {CONF_UNPUBLISHED_AS_UNKNOWN: True}
    coordinator.yesterday_complete = False
    coordinator.data = {
        "pod1": {"aggregated_data": {"actual_consumption": 12.3, "actual_supply": 0.0}}
    }
    sensor = SsdImsYesterdaySensor(coordinator, "actual_consumption", "pod1", "Home")
    assert sensor.native_value is None

    low = next(d for d in TREND_SENSORS if d[0] == "low_demand_kw")
    trend = SsdImsTrendSensor(coordinator, "pod1", "Home", low)
    coordinator.data = {"pod1": {"trends": {"low_demand_kw": 0.5}}}
    assert trend.native_value is None
