"""Tests for the SSD IMS config flow, using Home Assistant's own test
harness (pytest_homeassistant_custom_component) so the flow runs through
real hass.config_entries machinery instead of being hand-mocked."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ssd_ims.const import (
    CONF_HISTORY_DAYS,
    CONF_POD_NAME_MAPPING,
    CONF_POINT_OF_DELIVERY,
    DOMAIN,
)
from custom_components.ssd_ims.models import PointOfDelivery

# The integration declares "recorder" as a manifest dependency, so setting
# up the config flow (which loads the integration) needs the in-memory
# recorder test fixture, not just `hass`. recorder_mock must be listed
# before enable_custom_integrations: its internal setup has to run before
# `hass` is otherwise touched, and fixture order follows this order here.
pytestmark = pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")

POD_ID = "99XXX1234560000G"


def _mock_api_client(*, authenticated=True, pods=None):
    client = MagicMock()
    client.authenticate = AsyncMock(return_value=authenticated)
    client.get_points_of_delivery = AsyncMock(
        return_value=pods
        if pods is not None
        else [PointOfDelivery(text=f"{POD_ID} (Home)", value="v1")]
    )
    return client


async def _advance_to_pod_naming(hass: HomeAssistant, client) -> dict:
    with patch(
        "custom_components.ssd_ims.config_flow.SsdImsApiClient", return_value=client
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"username": "test_user", "password": "test_pass"},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"selected_pods": [POD_ID]},
        )
    return result


async def test_full_setup_flow_creates_entry(hass: HomeAssistant):
    """The happy path: credentials -> POD selection -> naming -> import
    settings -> a created config entry with the expected data."""
    client = _mock_api_client()

    with patch(
        "custom_components.ssd_ims.config_flow.SsdImsApiClient", return_value=client
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"username": "test_user", "password": "test_pass"},
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "point_of_delivery"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"selected_pods": [POD_ID]},
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "pod_naming"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {f"pod_name_{POD_ID}": "Home"},
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "history_import"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "SSD IMS (test_user)"
    assert result["data"][CONF_POINT_OF_DELIVERY] == [POD_ID]
    assert result["data"][CONF_POD_NAME_MAPPING] == {POD_ID: "Home"}
    assert result["data"][CONF_HISTORY_DAYS] > 0


async def test_invalid_credentials_shows_error(hass: HomeAssistant):
    client = _mock_api_client(authenticated=False)

    with patch(
        "custom_components.ssd_ims.config_flow.SsdImsApiClient", return_value=client
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"username": "test_user", "password": "wrong"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_portal_422_login_shows_invalid_auth(hass: HomeAssistant):
    """The portal rejects a wrong username/password with HTTP 422 (not
    401/403). Uses the real API client with only the HTTP session mocked, so
    a mistyped password shows "invalid_auth" rather than "cannot_connect"."""
    response = MagicMock()
    response.status = 422
    response.json = AsyncMock(
        return_value={
            "error": {
                "module": "AC",
                "code": "0x0100000F",
                "message": "Zadali ste nesprávne prihlasovacie meno alebo heslo.",
            }
        }
    )
    response.headers = {"content-type": "application/json"}
    response.cookies = {}
    session = MagicMock()
    session.post.return_value.__aenter__ = AsyncMock(return_value=response)
    session.post.return_value.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "custom_components.ssd_ims.config_flow.async_get_clientsession",
        return_value=session,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"username": "test_user", "password": "wrong"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_network_error_shows_cannot_connect(hass: HomeAssistant):
    client = MagicMock()
    client.authenticate = AsyncMock(side_effect=TimeoutError("timed out"))

    with patch(
        "custom_components.ssd_ims.config_flow.SsdImsApiClient", return_value=client
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"username": "test_user", "password": "test_pass"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_duplicate_account_aborts(hass: HomeAssistant):
    """Adding the same SSD IMS account a second time must be blocked."""
    MockConfigEntry(
        domain=DOMAIN,
        unique_id="test_user",
        data={"username": "test_user", "password": "test_pass"},
    ).add_to_hass(hass)

    client = _mock_api_client()

    with patch(
        "custom_components.ssd_ims.config_flow.SsdImsApiClient", return_value=client
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"username": "test_user", "password": "test_pass"},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_no_pods_selected_shows_error(hass: HomeAssistant):
    client = _mock_api_client()

    with patch(
        "custom_components.ssd_ims.config_flow.SsdImsApiClient", return_value=client
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"username": "test_user", "password": "test_pass"},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"selected_pods": []},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "point_of_delivery"
    assert result["errors"] == {"base": "no_pods_selected"}


async def test_pod_name_too_long_shows_error(hass: HomeAssistant):
    client = _mock_api_client()
    result = await _advance_to_pod_naming(hass, client)
    assert result["step_id"] == "pod_naming"

    with patch(
        "custom_components.ssd_ims.config_flow.SsdImsApiClient", return_value=client
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {f"pod_name_{POD_ID}": "x" * 51},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pod_naming"
    assert result["errors"] == {f"pod_name_{POD_ID}": "too_long"}


async def test_pod_name_invalid_format_shows_error(hass: HomeAssistant):
    """A name that sanitizes to an empty string (e.g. only punctuation)
    must be rejected rather than silently producing a blank statistic
    name."""
    client = _mock_api_client()
    result = await _advance_to_pod_naming(hass, client)

    with patch(
        "custom_components.ssd_ims.config_flow.SsdImsApiClient", return_value=client
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {f"pod_name_{POD_ID}": "!!!"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pod_naming"
    assert result["errors"] == {f"pod_name_{POD_ID}": "invalid_format"}


async def test_options_flow_updates_scan_interval(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test_user",
        data={
            "username": "test_user",
            "password": "test_pass",
            CONF_POINT_OF_DELIVERY: [POD_ID],
            CONF_POD_NAME_MAPPING: {},
        },
    )
    entry.add_to_hass(hass)

    coordinator = MagicMock()
    coordinator.update_config = AsyncMock()
    entry.runtime_data = coordinator

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"scan_interval": 720},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    coordinator.update_config.assert_called_once()
