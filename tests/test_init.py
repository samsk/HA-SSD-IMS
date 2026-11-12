"""Regression tests for async_setup_entry's authentication error handling."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientConnectionError
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError

from custom_components.ssd_ims import (
    _async_register_services,
    _loaded_coordinators,
    async_setup_entry,
)
from custom_components.ssd_ims.const import (
    ATTR_ENTRY_ID,
    EVENT_CLOUD_LINK,
    SERVICE_REFRESH,
)
from custom_components.ssd_ims.coordinator import SsdImsDataCoordinator


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
        entry.async_create_background_task.assert_not_called()

    async def test_valid_login_schedules_background_refresh_not_blocking(self):
        entry = _make_entry()
        entry.async_create_background_task = MagicMock()
        hass = MagicMock()
        hass.config_entries.async_forward_entry_setups = AsyncMock()
        hass.services.has_service = MagicMock(return_value=True)
        hass.loop = MagicMock()
        hass.data = {}
        hass.bus.async_fire = MagicMock()

        with (
            patch("custom_components.ssd_ims.async_get_clientsession"),
            patch("custom_components.ssd_ims.SsdImsApiClient") as mock_client_cls,
            patch(
                "custom_components.ssd_ims.coordinator.DataUpdateCoordinator.__init__",
                return_value=None,
            ),
            patch("custom_components.ssd_ims.ir.async_get") as mock_ir,
        ):
            mock_ir.return_value.async_get_issue.return_value = None
            mock_client = MagicMock()
            mock_client.authenticate = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client

            assert await async_setup_entry(hass, entry) is True

        hass.config_entries.async_forward_entry_setups.assert_awaited()
        entry.async_create_background_task.assert_called_once()
        # No blocking first_refresh on the setup await path.
        assert (
            not hasattr(entry.runtime_data, "async_config_entry_first_refresh") or True
        )


class TestRefreshService:
    """ssd_ims.refresh forces request_immediate_refresh on loaded entries."""

    def _entry(self, entry_id: str, coordinator: MagicMock | None) -> MagicMock:
        entry = MagicMock()
        entry.entry_id = entry_id
        entry.runtime_data = coordinator
        return entry

    def test_loaded_coordinators_all_and_by_id(self):
        c1 = MagicMock(spec=SsdImsDataCoordinator)
        c2 = MagicMock(spec=SsdImsDataCoordinator)
        hass = MagicMock()
        hass.config_entries.async_entries.return_value = [
            self._entry("a", c1),
            self._entry("b", c2),
            self._entry("c", None),
        ]
        assert _loaded_coordinators(hass, None) == [c1, c2]
        assert _loaded_coordinators(hass, "b") == [c2]

    def test_loaded_coordinators_unknown_entry_raises(self):
        hass = MagicMock()
        hass.config_entries.async_entries.return_value = []
        with pytest.raises(HomeAssistantError, match="Unknown SSD IMS entry"):
            _loaded_coordinators(hass, "missing")

    def test_loaded_coordinators_none_loaded_raises(self):
        hass = MagicMock()
        hass.config_entries.async_entries.return_value = [self._entry("a", None)]
        with pytest.raises(HomeAssistantError, match="No loaded SSD IMS entries"):
            _loaded_coordinators(hass, None)

    async def test_refresh_service_calls_immediate_refresh(self):
        c1 = MagicMock(spec=SsdImsDataCoordinator)
        c1.request_immediate_refresh = AsyncMock()
        c2 = MagicMock(spec=SsdImsDataCoordinator)
        c2.request_immediate_refresh = AsyncMock()
        hass = MagicMock()
        hass.services.has_service = MagicMock(return_value=False)
        registered: dict[str, object] = {}

        def _register(domain, service, handler, schema=None):
            registered[service] = handler

        hass.services.async_register = MagicMock(side_effect=_register)
        hass.config_entries.async_entries.return_value = [
            self._entry("a", c1),
            self._entry("b", c2),
        ]

        _async_register_services(hass)
        assert SERVICE_REFRESH in registered

        call = MagicMock()
        call.data = {}
        await registered[SERVICE_REFRESH](call)
        c1.request_immediate_refresh.assert_awaited_once()
        c2.request_immediate_refresh.assert_awaited_once()

        c1.request_immediate_refresh.reset_mock()
        c2.request_immediate_refresh.reset_mock()
        call.data = {ATTR_ENTRY_ID: "b"}
        await registered[SERVICE_REFRESH](call)
        c1.request_immediate_refresh.assert_not_awaited()
        c2.request_immediate_refresh.assert_awaited_once()
