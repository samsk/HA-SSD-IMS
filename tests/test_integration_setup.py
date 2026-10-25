"""End-to-end setup tests that exercise real Home Assistant machinery (the
real recorder, the real sensor entity platform) rather than mocking it
away. A real bug — an invalid sensor state_class for the ENERGY device
class — was only ever caught by running the integration in a real Home
Assistant instance; every unit test mocked away the exact piece (real
entity-platform validation) that would have caught it. This test closes
that gap.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import DOMAIN as HOMEASSISTANT_DOMAIN, HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ssd_ims.const import (
    CONF_HISTORY_DAYS,
    CONF_POD_NAME_MAPPING,
    CONF_POINT_OF_DELIVERY,
    DOMAIN,
)
from custom_components.ssd_ims.models import ChartData, PointOfDelivery

pytestmark = pytest.mark.usefixtures("recorder_mock", "enable_custom_integrations")

POD_ID = "99XXX1234560000G"


def _mock_api_client():
    client = MagicMock()
    client.authenticate = AsyncMock(return_value=True)
    client.get_points_of_delivery = AsyncMock(
        return_value=[PointOfDelivery(text=f"{POD_ID} (Home)", value="v1")]
    )
    client.get_chart_data = AsyncMock(
        return_value=ChartData(
            meteringDatetime=["2026-08-18T10:15:00Z"],
            actualConsumption=[1.5],
            actualSupply=[0.0],
            sumActualConsumption=1.5,
            sumActualSupply=0.0,
        )
    )
    client.set_cached_pods = MagicMock()
    return client


async def test_full_setup_against_real_recorder_and_sensor_platform(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
):
    """A real config entry, set up through hass.config_entries against the
    real recorder and real sensor platform, with only the network-facing
    API client mocked. Real sensor entities being added exercises HA's
    device_class/state_class compatibility validation, which the
    MEASUREMENT state class on the Yesterday sensor failed for the ENERGY
    device class.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id="test_user",
        data={
            "username": "test_user",
            "password": "test_pass",
            CONF_POINT_OF_DELIVERY: [POD_ID],
            CONF_POD_NAME_MAPPING: {POD_ID: "Home"},
            CONF_HISTORY_DAYS: 1,
        },
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.ssd_ims.SsdImsApiClient", return_value=_mock_api_client()
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED

    # Real entities were actually created, not just a "setup returned True".
    for entity_id in (
        "sensor.home_actual_consumption_yesterday",
        "sensor.home_actual_supply_yesterday",
        "sensor.home_actual_consumption_total",
        "sensor.home_actual_supply_total",
        "sensor.home_last_update",
    ):
        assert hass.states.get(entity_id) is not None, f"{entity_id} was not created"

    log_text = caplog.text
    assert "impossible" not in log_text, (
        "sensor state_class incompatible with its device_class"
    )
    assert "Traceback" not in log_text, f"unexpected error during setup:\n{log_text}"


def _create_stale_reauth_issue(hass: HomeAssistant, entry_id: str) -> str:
    """Simulate a reauth repair issue core created on a past auth failure."""
    issue_id = f"config_entry_reauth_{DOMAIN}_{entry_id}"
    ir.async_create_issue(
        hass,
        HOMEASSISTANT_DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="config_entry_reauth",
        translation_placeholders={"name": "SSD IMS"},
    )
    return issue_id


async def test_successful_setup_clears_stale_reauth_issue(hass: HomeAssistant):
    """A prior auth failure's repair issue may never get cleared by core if
    the entry recovers without the user completing the reauth flow (e.g. a
    transient portal-side 401/403 that resolves itself by the next restart).
    A successful setup should clear it regardless of how auth recovered.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id="test_user",
        data={
            "username": "test_user",
            "password": "test_pass",
            CONF_POINT_OF_DELIVERY: [POD_ID],
            CONF_POD_NAME_MAPPING: {POD_ID: "Home"},
            CONF_HISTORY_DAYS: 1,
        },
    )
    entry.add_to_hass(hass)

    issue_id = _create_stale_reauth_issue(hass, entry.entry_id)
    assert ir.async_get(hass).async_get_issue(HOMEASSISTANT_DOMAIN, issue_id)

    with patch(
        "custom_components.ssd_ims.SsdImsApiClient", return_value=_mock_api_client()
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert ir.async_get(hass).async_get_issue(HOMEASSISTANT_DOMAIN, issue_id) is None


async def test_removing_entry_clears_its_reauth_issue(hass: HomeAssistant):
    """Core's own cleanup only aborts in-progress reauth flows on removal —
    it doesn't delete the persisted repair issue — so a recreated integration
    would otherwise inherit a repair issue pointing at a dead entry_id.

    Goes through the real `hass.config_entries.async_remove` API (rather than
    calling our `async_remove_entry` hook directly) so the test actually
    proves Home Assistant invokes it during entry removal.
    """
    entry = MockConfigEntry(domain=DOMAIN, unique_id="test_user", data={})
    entry.add_to_hass(hass)

    issue_id = _create_stale_reauth_issue(hass, entry.entry_id)
    assert ir.async_get(hass).async_get_issue(HOMEASSISTANT_DOMAIN, issue_id)

    await hass.config_entries.async_remove(entry.entry_id)

    assert ir.async_get(hass).async_get_issue(HOMEASSISTANT_DOMAIN, issue_id) is None


def _session_rejecting_login():
    """An aiohttp session stand-in whose login POST gets the portal's real
    wrong-credentials response: HTTP 422 with an "error" body."""
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
    return session


async def test_password_rejected_with_422_starts_reauth_flow(hass: HomeAssistant):
    """Regression test for issue #25: after the user changed their password
    on the portal, login got a 422 which was treated as a transient error —
    setup retried forever (ConfigEntryNotReady) with the stale password and
    Home Assistant never offered a way to enter the new one. Runs the real
    API client (only the HTTP session is mocked) so the portal's actual
    status code flows through the whole setup path."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id="test_user",
        data={
            "username": "test_user",
            "password": "old_pass",
            CONF_POINT_OF_DELIVERY: [POD_ID],
            CONF_POD_NAME_MAPPING: {POD_ID: "Home"},
            CONF_HISTORY_DAYS: 1,
        },
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.ssd_ims.async_get_clientsession",
        return_value=_session_rejecting_login(),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]
    assert flows[0]["context"]["entry_id"] == entry.entry_id
