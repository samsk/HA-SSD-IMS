"""Regression tests for async_setup_entry's authentication error handling."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientConnectionError

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.exceptions import ConfigEntryNotReady

from custom_components.ssd_ims import async_setup_entry
from custom_components.ssd_ims.const import EVENT_CLOUD_LINK


def _make_entry():
    entry = MagicMock()
    entry.entry_id = "test-entry"
    entry.title = "user"
    entry.data = {CONF_USERNAME: "user", CONF_PASSWORD: "pass"}
    entry.options = {}
    entry.async_start_reauth = MagicMock()
    return entry


class TestAsyncSetupEntryAuthHandling:
    """A network/server failure must not be reported the same way as an
    actual bad-credentials failure: the former should let Home Assistant
    retry (ConfigEntryNotReady), the latter keeps the entry loaded and
    starts reauth with the cloud-link sensor available."""

    async def test_network_error_raises_config_entry_not_ready(self):
        entry = _make_entry()
        hass = MagicMock()

        with (
            patch("custom_components.ssd_ims.async_get_clientsession"),
            patch("custom_components.ssd_ims.SsdImsApiClient") as mock_client_cls,
        ):
            mock_client = MagicMock()
            mock_client.authenticate = AsyncMock(
                side_effect=ClientConnectionError("network down")
            )
            mock_client_cls.return_value = mock_client

            with pytest.raises(ConfigEntryNotReady):
                await async_setup_entry(hass, entry)

    async def test_invalid_credentials_keeps_entry_loaded(self):
        entry = _make_entry()
        hass = MagicMock()
        hass.config_entries.async_forward_entry_setups = AsyncMock()
        hass.services.has_service = MagicMock(return_value=True)
        # DataUpdateCoordinator needs a few hass helpers during __init__.
        hass.loop = MagicMock()
        hass.data = {}

        with (
            patch("custom_components.ssd_ims.async_get_clientsession"),
            patch("custom_components.ssd_ims.SsdImsApiClient") as mock_client_cls,
            patch(
                "custom_components.ssd_ims.coordinator.DataUpdateCoordinator.__init__",
                return_value=None,
            ),
        ):
            mock_client = MagicMock()
            mock_client.authenticate = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            assert await async_setup_entry(hass, entry) is True

        assert entry.runtime_data.cloud_link_ok is False
        entry.async_start_reauth.assert_called_once_with(hass)
        hass.bus.async_fire.assert_called_with(
            EVENT_CLOUD_LINK, {"entry_id": "test-entry", "ok": False}
        )
        hass.config_entries.async_forward_entry_setups.assert_awaited()
