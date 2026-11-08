"""SSD IMS Home Assistant integration."""

import logging
import re

from aiohttp import ClientError
from pydantic import ValidationError

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import DOMAIN as HOMEASSISTANT_DOMAIN, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api_client import SsdImsApiClient
from .const import (
    CONF_HISTORY_DAYS,
    CONF_POD_NAME_MAPPING,
    CONF_POINT_OF_DELIVERY,
    CONF_PRICE_NT,
    CONF_PRICE_VT,
    CONF_SCAN_INTERVAL,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_POINT_OF_DELIVERY,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .coordinator import SsdImsDataCoordinator
from .models import PointOfDelivery

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]

type SsdImsConfigEntry = ConfigEntry[SsdImsDataCoordinator]


def _async_clear_reauth_issue(hass: HomeAssistant, entry_id: str) -> None:
    """Delete this entry's reauth repair issue, if one exists.

    Home Assistant core only clears a `config_entry_reauth_<domain>_<entry_id>`
    repair issue when a reauth *flow* for that entry is completed or aborted
    (see core's `ConfigEntriesFlowManager.async_flow_removed`). It does not
    clear the issue when the entry is removed, nor when authentication simply
    starts succeeding again through some other path (e.g. a transient
    portal-side 401/403 that clears up by the next HA restart's automatic
    `authenticate()` call, without the user ever opening the reauth flow).
    Both cases would otherwise leave a stale issue behind indefinitely, so we
    clear it ourselves. `async_delete_issue` is a no-op if the issue doesn't
    exist.
    """
    ir.async_delete_issue(
        hass, HOMEASSISTANT_DOMAIN, f"config_entry_reauth_{DOMAIN}_{entry_id}"
    )


async def _async_get_pods(
    hass: HomeAssistant, username: str, password: str
) -> list[PointOfDelivery] | None:
    """Authenticate and fetch PODs for migration."""
    api_client = SsdImsApiClient(async_get_clientsession(hass))

    if not await api_client.authenticate(username, password):
        return None

    return await api_client.get_points_of_delivery()


async def async_setup_entry(hass: HomeAssistant, entry: SsdImsConfigEntry) -> bool:
    """Set up SSD IMS from a config entry."""
    api_client = SsdImsApiClient(async_get_clientsession(hass))

    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]

    try:
        authenticated = await api_client.authenticate(username, password)
    except (ClientError, TimeoutError, RuntimeError, ValidationError) as err:
        # Network/timeout/server/unexpected-response problems are transient
        # or portal-side, not proof the credentials are wrong — let HA retry
        # instead of forcing the user through a spurious reauth flow.
        raise ConfigEntryNotReady(f"Cannot connect to SSD IMS: {err}") from err

    if not authenticated:
        raise ConfigEntryAuthFailed("Authentication failed for SSD IMS")

    # Setup just succeeded, so whatever previously triggered a reauth-required
    # repair issue for this entry (if any) is resolved now, even if the user
    # never went through the reauth flow itself.
    _async_clear_reauth_issue(hass, entry.entry_id)

    # entry.options takes precedence over entry.data for user-adjustable settings
    scan_interval = entry.options.get(
        CONF_SCAN_INTERVAL,
        entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
    )

    config = {
        CONF_USERNAME: username,
        CONF_PASSWORD: password,
        CONF_SCAN_INTERVAL: scan_interval,
        CONF_POINT_OF_DELIVERY: entry.data.get(
            CONF_POINT_OF_DELIVERY, DEFAULT_POINT_OF_DELIVERY
        ),
        CONF_POD_NAME_MAPPING: entry.data.get(CONF_POD_NAME_MAPPING, {}),
        CONF_HISTORY_DAYS: entry.data.get(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS),
    }
    # Optional tariff prices only ever live in entry.options
    for price_key in (CONF_PRICE_VT, CONF_PRICE_NT):
        if (price := entry.options.get(price_key)) is not None:
            config[price_key] = price

    coordinator = SsdImsDataCoordinator(hass, api_client, config, entry)
    entry.runtime_data = coordinator

    # First refresh populates coordinator.data before platforms register entities
    await coordinator.async_config_entry_first_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: SsdImsConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        entry.runtime_data.async_unwatch_supplier_prices()
        entry.runtime_data = None

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: SsdImsConfigEntry) -> None:
    """Clean up when a config entry is removed.

    Core aborts any in-progress reauth *flow* for the removed entry, but
    leaves a persisted reauth repair issue in place if one exists (see
    `_async_clear_reauth_issue`), which would otherwise reference a dead
    entry_id forever.
    """
    _async_clear_reauth_issue(hass, entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate config entry if needed."""
    if entry.version == 1:
        # Migrate from POD IDs/texts to stable POD IDs
        data = dict(entry.data)
        point_of_delivery = data.get(CONF_POINT_OF_DELIVERY, DEFAULT_POINT_OF_DELIVERY)

        # Check if we have old POD ID format (long strings that look like session tokens)
        if point_of_delivery and any(len(pod) > 50 for pod in point_of_delivery):
            _LOGGER.info("Migrating from session POD IDs to stable POD IDs")

            try:
                username = data[CONF_USERNAME]
                password = data[CONF_PASSWORD]

                pods = await _async_get_pods(hass, username, password)
                if pods is not None:
                    # get_points_of_delivery() already filters out any POD it
                    # couldn't parse a stable ID for.
                    pod_mapping = {
                        pod.value: pod.id for pod in pods
                    }  # session_id -> stable_id

                    _LOGGER.debug(
                        "Available PODs for migration: %s",
                        list(pod_mapping.values()),
                    )

                    new_point_of_delivery = []
                    for session_pod_id in point_of_delivery:
                        if session_pod_id in pod_mapping:
                            stable_pod_id = pod_mapping[session_pod_id]
                            new_point_of_delivery.append(stable_pod_id)
                            _LOGGER.info(
                                "Migrated session POD ID %s to stable ID %s",
                                session_pod_id,
                                stable_pod_id,
                            )
                        else:
                            _LOGGER.warning(
                                "Session POD ID %s not found in current PODs, removing",
                                session_pod_id,
                            )

                    data[CONF_POINT_OF_DELIVERY] = new_point_of_delivery
                else:
                    _LOGGER.error("Failed to authenticate during migration")
                    return False

            except Exception as e:
                _LOGGER.error("Error during configuration migration: %s", e)
                return False

        # Also check for POD texts that need to be converted to stable IDs
        elif point_of_delivery:
            _LOGGER.debug("Checking for POD text to stable ID conversion")

            try:
                username = data[CONF_USERNAME]
                password = data[CONF_PASSWORD]

                pods = await _async_get_pods(hass, username, password)
                if pods is None:
                    _LOGGER.error(
                        "Failed to authenticate during POD text migration; "
                        "skipping version bump so migration can be retried"
                    )
                    return False

                # get_points_of_delivery() already filters out any POD it
                # couldn't parse a stable ID for.
                pod_text_to_id = {pod.text: pod.id for pod in pods}  # text -> stable_id

                _LOGGER.debug(
                    "Available POD stable IDs: %s",
                    list(pod_text_to_id.values()),
                )
                _LOGGER.debug("Configured POD texts: %s", point_of_delivery)

                missing_pods = [
                    pod for pod in point_of_delivery if pod not in pod_text_to_id
                ]
                if missing_pods:
                    _LOGGER.warning(
                        "Some configured POD texts not found in current API response: %s",
                        missing_pods,
                    )

                    updated_point_of_delivery = []
                    for pod_text in point_of_delivery:
                        if pod_text in pod_text_to_id:
                            stable_id = pod_text_to_id[pod_text]
                            updated_point_of_delivery.append(stable_id)
                            _LOGGER.info(
                                "Converted POD text %s to stable ID %s",
                                pod_text,
                                stable_id,
                            )
                        else:
                            match = re.search(r"^([A-Z0-9]+)", pod_text)
                            if match:
                                pod_number = match.group(1)
                                for (
                                    current_pod_text,
                                    stable_id,
                                ) in pod_text_to_id.items():
                                    if current_pod_text.startswith(pod_number):
                                        updated_point_of_delivery.append(stable_id)
                                        _LOGGER.info(
                                            "Updated POD from %s to stable ID %s",
                                            pod_text,
                                            stable_id,
                                        )
                                        break
                                else:
                                    _LOGGER.warning(
                                        "No matching POD found for %s", pod_text
                                    )
                            else:
                                _LOGGER.warning(
                                    "Could not extract POD number from %s", pod_text
                                )
                else:
                    updated_point_of_delivery = []
                    for pod_text in point_of_delivery:
                        if pod_text in pod_text_to_id:
                            stable_id = pod_text_to_id[pod_text]
                            updated_point_of_delivery.append(stable_id)
                            _LOGGER.info(
                                "Converted POD text %s to stable ID %s",
                                pod_text,
                                stable_id,
                            )
                        else:
                            _LOGGER.warning("POD text %s not found, removing", pod_text)

                if updated_point_of_delivery != point_of_delivery:
                    data[CONF_POINT_OF_DELIVERY] = updated_point_of_delivery
                    _LOGGER.info("POD text to stable ID conversion completed")

            except Exception as e:
                _LOGGER.error("Error during POD text to stable ID conversion: %s", e)
                # Don't fail the migration for this, just log the error

        hass.config_entries.async_update_entry(entry, data=data, version=2)
        _LOGGER.info("Configuration migration to version 2 completed")
        return True

    return False
