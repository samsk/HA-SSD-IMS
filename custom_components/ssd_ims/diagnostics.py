"""Diagnostics support for SSD IMS integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import SsdImsConfigEntry
from .const import CONF_POD_NAME_MAPPING, CONF_POINT_OF_DELIVERY

# POD IDs are stable utility-meter numbers, and friendly names are
# user-supplied free text that the config flow's own onboarding copy
# suggests filling in with a home address — neither belongs in a
# diagnostics dump that may get attached to a public support request.
TO_REDACT = [
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_POINT_OF_DELIVERY,
    CONF_POD_NAME_MAPPING,
]


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SsdImsConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data

    pods = getattr(coordinator, "pods", None) or {}
    # Map each real pod_id to an opaque per-diagnostics label instead of the
    # underlying meter/POD number, for the same reason as TO_REDACT above.
    pod_labels = {pod_id: f"pod_{i + 1}" for i, pod_id in enumerate(pods)}

    coordinator_data: dict[str, Any] = {}
    for pod_id, pod_data in (getattr(coordinator, "data", None) or {}).items():
        label = pod_labels.get(pod_id, "pod_unknown")
        coordinator_data[label] = {
            "last_update": pod_data.get("last_update"),
            "cumulative_totals": pod_data.get("cumulative_totals"),
            "aggregated_data": pod_data.get("aggregated_data"),
            # Meter serial number is identifying, like the POD number.
            "registry": {
                k: v
                for k, v in (pod_data.get("registry") or {}).items()
                if k != "serial_number"
            },
        }

    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "pods_discovered": list(pod_labels.values()),
        "coordinator_data": coordinator_data,
    }
