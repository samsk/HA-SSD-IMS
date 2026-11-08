"""Regression tests for diagnostics redaction of POD identifiers/names."""

from unittest.mock import MagicMock

from custom_components.ssd_ims.diagnostics import async_get_config_entry_diagnostics

REAL_POD_ID = "99XXX1234560000G"
REAL_FRIENDLY_NAME = "123 Main Street"


async def test_diagnostics_never_expose_raw_pod_id_or_friendly_name():
    """A diagnostics dump may be attached to a public support request, so it
    must not leak the utility meter/POD number or a friendly name the user
    may have set to their home address."""
    entry = MagicMock()
    entry.data = {
        "username": "someone@example.com",
        "password": "hunter2",
        "point_of_delivery": [REAL_POD_ID],
        "pod_name_mapping": {REAL_POD_ID: REAL_FRIENDLY_NAME},
    }
    entry.options = {}

    coordinator = MagicMock()
    coordinator.pods = {REAL_POD_ID: MagicMock()}
    coordinator.data = {
        REAL_POD_ID: {
            "last_update": "2026-01-01T00:00:00+00:00",
            "cumulative_totals": {"actual_consumption": 42.0},
            "aggregated_data": {},
        }
    }
    entry.runtime_data = coordinator

    diagnostics = await async_get_config_entry_diagnostics(MagicMock(), entry)

    dump = str(diagnostics)
    assert REAL_POD_ID not in dump
    assert REAL_FRIENDLY_NAME not in dump
    assert "hunter2" not in dump

    # Structure/values should still be present under an opaque label, so
    # diagnostics stay useful for debugging.
    assert diagnostics["pods_discovered"] == ["pod_1"]
    assert diagnostics["coordinator_data"]["pod_1"]["cumulative_totals"] == {
        "actual_consumption": 42.0
    }


async def test_diagnostics_include_register_reading_without_meter_serial():
    entry = MagicMock()
    entry.data = {"point_of_delivery": [REAL_POD_ID], "pod_name_mapping": {}}
    entry.options = {"price_vt": 0.25}

    coordinator = MagicMock()
    coordinator.pods = {REAL_POD_ID: MagicMock()}
    coordinator.data = {
        REAL_POD_ID: {
            "registry": {
                "reading_time": "2026-10-31T23:00:00+00:00",
                "serial_number": "340099999",
                "consumption_vt": 350.7,
            }
        }
    }
    entry.runtime_data = coordinator

    diagnostics = await async_get_config_entry_diagnostics(MagicMock(), entry)

    assert "340099999" not in str(diagnostics)
    assert diagnostics["coordinator_data"]["pod_1"]["registry"]["consumption_vt"] == (
        350.7
    )
