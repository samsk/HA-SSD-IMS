"""Tests for tariff register readings (Registrové údaje): parsing, API client,
counter → delta conversion and statistics import."""

import asyncio
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientSession

from custom_components.ssd_ims.api_client import SsdImsApiClient
from custom_components.ssd_ims.const import (
    REGISTRY_HISTORY_START,
    CONF_POD_NAME_MAPPING,
    CONF_PRICE_NT,
    CONF_PRICE_VT,
)
from custom_components.ssd_ims.coordinator import SsdImsDataCoordinator
from custom_components.ssd_ims.models import REGISTRY_COLUMNS, RegistryReading

POD_ID = "99XXX1234560000G"


def _reading(
    when: datetime,
    vt: float,
    nt: float,
    serial: str = "M1",
    max_demand: float = 3.0,
) -> RegistryReading:
    return RegistryReading(
        serial_number=serial,
        reading_time=when,
        max_demand=max_demand,
        consumption_total=vt + nt,
        consumption_tariff1=vt,
        consumption_tariff2=nt,
        supply_total=0.0,
    )


class TestRegistryReadingModel:
    def test_from_row_parses_portal_row(self):
        row = [
            "340016976",
            "2026-10-31T23:00:00.0000000Z",
            3.244,
            6295.263,
            350.743,
            5944.52,
            0,
        ]
        reading = RegistryReading.from_row(row)
        assert reading.serial_number == "340016976"
        assert reading.reading_time == datetime(2026, 10, 31, 23, tzinfo=UTC)
        assert reading.max_demand == 3.244
        assert reading.consumption_tariff1 == 350.743
        assert reading.consumption_tariff2 == 5944.52
        assert reading.supply_total == 0

    def test_from_row_keeps_seconds_of_ad_hoc_reading(self):
        reading = RegistryReading.from_row(
            ["1", "2026-10-24T18:21:10.0000000Z", None, None, 1.0, 2.0, None]
        )
        assert reading.reading_time == datetime(2026, 10, 24, 18, 21, 10, tzinfo=UTC)
        assert reading.max_demand is None

    def test_from_row_rejects_short_row(self):
        with pytest.raises(ValueError):
            RegistryReading.from_row(["1", "2026-10-31T23:00:00Z"])


class TestGetRegistryData:
    @pytest.fixture
    async def api_client(self):
        session = ClientSession()
        client = SsdImsApiClient(session)
        client._authenticated = True
        pod = MagicMock()
        pod.id = POD_ID
        pod.value = "session_value"
        pod.text = f"{POD_ID} (Home)"
        client._pods_cache = [pod]
        client._pods_cache_ts = datetime.now(UTC)
        yield client
        await session.close()

    async def test_request_payload_and_sorted_parsing(self, api_client, caplog):
        response_body = {
            "columns": [],
            "page": {"totalRows": 3, "currentPage": 1, "pageSize": 500},
            "rows": [
                {"values": ["M1", "2026-10-31T23:00:00.0000000Z", 3, 30, 3, 27, 0]},
                {"values": ["M1", "2026-09-30T22:00:00.0000000Z", 2, 20, 2, 18, 0]},
                {"values": ["garbage"]},
            ],
        }
        with patch.object(api_client._session, "request") as mock_request:
            mock_response = AsyncMock()
            mock_response.json = AsyncMock(return_value=response_body)
            mock_response.status = 200
            mock_response.headers = {"content-type": "application/json"}
            mock_request.return_value.__aenter__.return_value = mock_response

            readings = await api_client.get_registry_data(
                POD_ID,
                datetime(2025, 10, 1, tzinfo=UTC),
                datetime(2026, 11, 9, tzinfo=UTC),
            )

        # Unparseable row skipped (without logging it — rows contain the
        # meter serial), remaining rows sorted by time.
        assert "garbage" not in caplog.text
        # Unparseable row skipped, remaining rows sorted by time.
        assert [r.consumption_tariff1 for r in readings] == [2, 3]

        payload = mock_request.call_args.kwargs["json"]
        assert [c["member"] for c in payload["columns"]] == list(REGISTRY_COLUMNS)
        pod_filter = payload["filters"][0]
        assert pod_filter["member"] == "pointOfDeliveryId"
        assert pod_filter["value"] == "session_value"
        assert payload["filters"][1]["value"] == "2025-10-01T00:00:00.000Z"
        assert payload["filters"][2]["value"] == "2026-11-09T00:00:00.000Z"

    async def test_unexpected_response_raises(self, api_client):
        with patch.object(api_client._session, "request") as mock_request:
            mock_response = AsyncMock()
            mock_response.json = AsyncMock(return_value={"unexpected": True})
            mock_response.status = 200
            mock_response.headers = {"content-type": "application/json"}
            mock_request.return_value.__aenter__.return_value = mock_response

            with pytest.raises(RuntimeError):
                await api_client.get_registry_data(
                    POD_ID, datetime(2025, 1, 1, tzinfo=UTC), datetime.now(UTC)
                )


class TestRegistryDeltas:
    def test_hour_aligned_reading_closes_previous_hour(self):
        # 00:00 local (Bratislava, CET) on 1 Nov = 23:00 UTC on 31 Oct
        bucket = SsdImsDataCoordinator._registry_bucket(
            datetime(2026, 10, 31, 23, tzinfo=UTC)
        )
        assert bucket == datetime(2026, 10, 31, 22, tzinfo=UTC)

    def test_ad_hoc_reading_belongs_to_its_own_hour(self):
        bucket = SsdImsDataCoordinator._registry_bucket(
            datetime(2026, 10, 24, 18, 21, 10, tzinfo=UTC)
        )
        assert bucket == datetime(2026, 10, 24, 18, tzinfo=UTC)

    def test_counters_become_deltas_including_mid_month_reading(self):
        readings = [
            _reading(datetime(2026, 10, 31, 23, tzinfo=UTC), 350.743, 5944.52),
            _reading(datetime(2026, 9, 30, 22, tzinfo=UTC), 329.492, 5725.716),
            _reading(datetime(2026, 10, 24, 18, 21, 10, tzinfo=UTC), 345.081, 5879.8),
        ]
        deltas = SsdImsDataCoordinator._registry_deltas(readings, "consumption_tariff1")
        assert [b for b, _ in deltas] == [
            datetime(2026, 10, 24, 18, tzinfo=UTC),
            datetime(2026, 10, 31, 22, tzinfo=UTC),
        ]
        assert deltas[0][1] == pytest.approx(345.081 - 329.492)
        assert deltas[1][1] == pytest.approx(350.743 - 345.081)

    def test_no_delta_across_meter_replacement(self):
        readings = [
            _reading(datetime(2026, 1, 31, 23, tzinfo=UTC), 100, 1000),
            _reading(datetime(2026, 2, 28, 23, tzinfo=UTC), 5, 50, serial="M2"),
            _reading(datetime(2026, 3, 31, 22, tzinfo=UTC), 15, 150, serial="M2"),
        ]
        deltas = SsdImsDataCoordinator._registry_deltas(readings, "consumption_tariff2")
        assert deltas == [(datetime(2026, 3, 31, 21, tzinfo=UTC), 100)]

    def test_reset_log_never_contains_meter_serials(self, caplog):
        readings = [
            _reading(datetime(2026, 1, 31, 23, tzinfo=UTC), 100, 1000, serial="S111"),
            _reading(datetime(2026, 2, 28, 23, tzinfo=UTC), 5, 50, serial="S222"),
        ]
        with caplog.at_level("INFO"):
            SsdImsDataCoordinator._registry_deltas(readings, "consumption_tariff1")
        assert "meter replaced" in caplog.text
        assert "S111" not in caplog.text
        assert "S222" not in caplog.text

    def test_counter_going_backwards_is_skipped(self):
        readings = [
            _reading(datetime(2026, 1, 31, 23, tzinfo=UTC), 100, 1000),
            _reading(datetime(2026, 2, 28, 23, tzinfo=UTC), 90, 1100),
        ]
        assert (
            SsdImsDataCoordinator._registry_deltas(readings, "consumption_tariff1")
            == []
        )

    def test_missing_values_are_ignored(self):
        readings = [
            _reading(datetime(2026, 1, 31, 23, tzinfo=UTC), 100, 1000),
            RegistryReading(
                serial_number="M1", reading_time=datetime(2026, 2, 28, 23, tzinfo=UTC)
            ),
            _reading(datetime(2026, 3, 31, 22, tzinfo=UTC), 130, 1300),
        ]
        deltas = SsdImsDataCoordinator._registry_deltas(readings, "consumption_tariff1")
        assert deltas == [(datetime(2026, 3, 31, 21, tzinfo=UTC), 30)]


def _make_coordinator(api_client, config, data=None):
    coordinator = SsdImsDataCoordinator.__new__(SsdImsDataCoordinator)
    coordinator.hass = MagicMock()
    # HA currency deliberately not EUR: costs must still be in EUR
    coordinator.hass.config.currency = "GBP"
    coordinator.api_client = api_client
    coordinator.config = config
    coordinator.entry = MagicMock()
    coordinator.pods = {POD_ID: MagicMock()}
    coordinator._last_successful_data_date = None
    coordinator._stats_lock = asyncio.Lock()
    coordinator._registry_lock = asyncio.Lock()
    coordinator._last_registry_fetch_date = None
    coordinator._registry_readings = {}
    coordinator._supplier_entity_ids = frozenset()
    coordinator._unsub_supplier = None
    coordinator._reprice_requested = False
    coordinator.data = data
    # No supplier integration installed unless a test says otherwise
    coordinator._supplier_prices = MagicMock(return_value=None)
    return coordinator


async def _run_executor(fn, *args, **kwargs):
    return fn(*args, **kwargs)


SEP = datetime(2026, 8, 31, 22, tzinfo=UTC)  # 1 Sep 00:00 local
OCT = datetime(2026, 9, 30, 22, tzinfo=UTC)  # 1 Oct 00:00 local
NOV = datetime(2026, 10, 31, 23, tzinfo=UTC)  # 1 Nov 00:00 local
READINGS = [_reading(SEP, 10, 100), _reading(OCT, 20, 210), _reading(NOV, 25, 300)]


async def _run_registry_update(coordinator, last_stats=None, today=date(2026, 11, 8)):
    """Run _update_registry with recorder access mocked; return (data, add mock)."""
    last_stats = last_stats or {}

    def fake_last_stats(hass, n, statistic_id, convert, types):
        return last_stats.get(statistic_id, {})

    all_pod_data: dict = {POD_ID: {}}
    with (
        patch(
            "custom_components.ssd_ims.coordinator.get_instance"
        ) as mock_get_instance,
        patch(
            "custom_components.ssd_ims.coordinator.get_last_statistics",
            side_effect=fake_last_stats,
        ),
        patch(
            "custom_components.ssd_ims.coordinator.async_add_external_statistics"
        ) as mock_add_stats,
    ):
        mock_instance = MagicMock()
        mock_instance.async_add_executor_job = AsyncMock(side_effect=_run_executor)
        mock_get_instance.return_value = mock_instance
        await coordinator._update_registry([POD_ID], all_pod_data, today)
    return all_pod_data, mock_add_stats


def _imported(mock_add_stats) -> dict[str, list[dict]]:
    return {
        call.args[1]["statistic_id"]: call.args[2]
        for call in mock_add_stats.call_args_list
    }


class TestRegistryImport:
    async def test_first_import_energy_and_cost(self):
        client = MagicMock()
        client.get_registry_data = AsyncMock(return_value=READINGS)
        coordinator = _make_coordinator(
            client,
            {
                CONF_POD_NAME_MAPPING: {POD_ID: "Home"},
                CONF_PRICE_VT: 0.25,
                CONF_PRICE_NT: 0.1,
            },
        )

        data, mock_add_stats = await _run_registry_update(coordinator)
        imported = _imported(mock_add_stats)

        vt = imported["ssd_ims:home_consumption_vt"]
        assert [s["start"] for s in vt] == [
            datetime(2026, 9, 30, 21, tzinfo=UTC),
            datetime(2026, 10, 31, 22, tzinfo=UTC),
        ]
        assert [s["sum"] for s in vt] == [10, 15]
        assert [s["sum"] for s in imported["ssd_ims:home_consumption_nt"]] == [
            110,
            200,
        ]
        assert [s["sum"] for s in imported["ssd_ims:home_cost_vt"]] == [2.5, 3.75]
        assert [s["sum"] for s in imported["ssd_ims:home_cost_nt"]] == pytest.approx(
            [11.0, 20.0]
        )

        cost_meta = next(
            c.args[1]
            for c in mock_add_stats.call_args_list
            if c.args[1]["statistic_id"] == "ssd_ims:home_cost_vt"
        )
        assert cost_meta["unit_of_measurement"] == "EUR"
        assert cost_meta["unit_class"] is None

        registry = data[POD_ID]["registry"]
        assert registry["consumption_vt"] == 25
        assert registry["consumption_nt"] == 300
        assert registry["max_demand"] == 3.0
        assert registry["serial_number"] == "M1"
        assert coordinator._last_registry_fetch_date == date(2026, 11, 8)
        # Always requests the full history, so the first import is complete.
        assert client.get_registry_data.call_args.args[1] == REGISTRY_HISTORY_START

    async def test_prices_changed_during_the_fetch_are_used(self):
        """Imported buckets are never repriced, so the import must use the
        prices that are current once the portal has answered."""
        coordinator = _make_coordinator(MagicMock(), {CONF_PRICE_VT: 0.25})

        async def fetch(*args):
            coordinator.config = {CONF_PRICE_VT: 0.5}
            return READINGS

        coordinator.api_client.get_registry_data = AsyncMock(side_effect=fetch)

        _, mock_add_stats = await _run_registry_update(coordinator)

        cost_vt = _imported(mock_add_stats)[f"ssd_ims:{POD_ID.lower()}_cost_vt"]
        assert [s["sum"] for s in cost_vt] == [5.0, 7.5]

    async def test_no_cost_statistics_without_prices(self):
        client = MagicMock()
        client.get_registry_data = AsyncMock(return_value=READINGS)
        coordinator = _make_coordinator(client, {})

        _, mock_add_stats = await _run_registry_update(coordinator)

        assert set(_imported(mock_add_stats)) == {
            f"ssd_ims:{POD_ID.lower()}_consumption_vt",
            f"ssd_ims:{POD_ID.lower()}_consumption_nt",
        }

    async def test_only_newer_deltas_are_appended_to_existing_sum(self):
        client = MagicMock()
        client.get_registry_data = AsyncMock(return_value=READINGS)
        coordinator = _make_coordinator(
            client, {CONF_POD_NAME_MAPPING: {POD_ID: "Home"}}
        )
        last = {
            "ssd_ims:home_consumption_vt": {
                "ssd_ims:home_consumption_vt": [
                    {
                        "start": datetime(2026, 9, 30, 21, tzinfo=UTC).timestamp(),
                        "sum": 10.0,
                    }
                ]
            }
        }

        _, mock_add_stats = await _run_registry_update(coordinator, last_stats=last)
        vt = _imported(mock_add_stats)["ssd_ims:home_consumption_vt"]

        assert vt == [{"start": datetime(2026, 10, 31, 22, tzinfo=UTC), "sum": 15.0}]

    async def test_nothing_new_imports_nothing(self):
        client = MagicMock()
        client.get_registry_data = AsyncMock(return_value=READINGS)
        coordinator = _make_coordinator(
            client, {CONF_POD_NAME_MAPPING: {POD_ID: "Home"}}
        )
        latest = {"start": datetime(2026, 10, 31, 22, tzinfo=UTC).timestamp(), "sum": 1}
        last = {
            sid: {sid: [latest]}
            for sid in ("ssd_ims:home_consumption_vt", "ssd_ims:home_consumption_nt")
        }

        _, mock_add_stats = await _run_registry_update(coordinator, last_stats=last)

        mock_add_stats.assert_not_called()

    async def test_fetched_once_per_day_and_previous_reading_carried_over(self):
        client = MagicMock()
        client.get_registry_data = AsyncMock(return_value=READINGS)
        previous = {"consumption_vt": 1.0}
        coordinator = _make_coordinator(
            client, {}, data={POD_ID: {"registry": previous}}
        )
        coordinator._last_registry_fetch_date = date(2026, 11, 8)

        data, mock_add_stats = await _run_registry_update(coordinator)

        client.get_registry_data.assert_not_called()
        mock_add_stats.assert_not_called()
        assert data[POD_ID]["registry"] is previous

    async def test_fetch_error_keeps_previous_reading_and_retries(self):
        client = MagicMock()
        client.get_registry_data = AsyncMock(side_effect=RuntimeError("portal down"))
        previous = {"consumption_vt": 1.0}
        coordinator = _make_coordinator(
            client, {}, data={POD_ID: {"registry": previous}}
        )

        data, _ = await _run_registry_update(coordinator)

        assert data[POD_ID]["registry"] is previous
        assert coordinator._last_registry_fetch_date is None

    async def test_update_config_resets_registry_gate(self):
        coordinator = _make_coordinator(MagicMock(), {})
        coordinator.update_interval = None
        coordinator._last_registry_fetch_date = date(2026, 11, 8)

        await coordinator.update_config({CONF_PRICE_VT: 0.3})

        assert coordinator._last_registry_fetch_date is None


class TestBlendedPrice:
    PERIODS = SsdImsDataCoordinator._register_periods(
        [
            _reading(SEP, 10, 100),
            _reading(OCT, 20, 190),  # Sep: 10 VT / 90 NT -> 10 % VT
            _reading(NOV, 20, 290),  # Oct: 0 VT / 100 NT -> 0 % VT
        ]
    )

    def _price(self, hour_start):
        return SsdImsDataCoordinator._blended_price(
            self.PERIODS, hour_start, price_vt=1.0, price_nt=0.0
        )

    def test_periods_carry_vt_share(self):
        assert [p[2] for p in self.PERIODS] == [pytest.approx(0.1), 0.0]

    def test_hour_uses_its_periods_mix(self):
        assert self._price(datetime(2026, 9, 15, 10, tzinfo=UTC)) == pytest.approx(0.1)
        assert self._price(datetime(2026, 10, 15, 10, tzinfo=UTC)) == 0.0

    def test_hour_closed_by_reading_belongs_to_that_period(self):
        # 21:00-22:00 UTC on 30 Sep ends exactly at the 1 Oct 00:00 reading
        assert self._price(datetime(2026, 9, 30, 21, tzinfo=UTC)) == pytest.approx(0.1)
        assert self._price(datetime(2026, 9, 30, 22, tzinfo=UTC)) == 0.0

    def test_hours_outside_known_periods_use_nearest_mix(self):
        assert self._price(datetime(2026, 11, 5, 10, tzinfo=UTC)) == 0.0
        assert self._price(datetime(2026, 8, 1, 10, tzinfo=UTC)) == pytest.approx(0.1)

    def test_period_without_consumption_falls_back_to_earlier_mix(self):
        periods = SsdImsDataCoordinator._register_periods(
            [_reading(SEP, 10, 100), _reading(OCT, 20, 190), _reading(NOV, 20, 190)]
        )
        assert periods[1][2] is None
        price = SsdImsDataCoordinator._blended_price(
            periods, datetime(2026, 10, 15, tzinfo=UTC), 1.0, 0.0
        )
        assert price == pytest.approx(0.1)

    def test_meter_replacement_period_is_skipped(self):
        periods = SsdImsDataCoordinator._register_periods(
            [_reading(SEP, 10, 100), _reading(OCT, 1, 5, serial="M2")]
        )
        assert periods == []


class TestHourlyCostStartupGuard:
    async def test_first_refresh_never_waits_for_the_recorder(self):
        """Waiting for the recorder during the setup refresh deadlocks Home
        Assistant's startup (the recorder only commits once startup is done)."""
        coordinator = _make_coordinator(
            MagicMock(), {CONF_PRICE_VT: 0.3, CONF_PRICE_NT: 0.1}, data=None
        )
        with patch(
            "custom_components.ssd_ims.coordinator.get_instance"
        ) as mock_get_instance:
            await coordinator._update_hourly_costs([POD_ID])
        mock_get_instance.assert_not_called()

    async def test_not_running_yet_never_waits_for_the_recorder(self):
        from homeassistant.core import CoreState

        coordinator = _make_coordinator(
            MagicMock(), {CONF_PRICE_VT: 0.3, CONF_PRICE_NT: 0.1}, data={}
        )
        coordinator.hass.state = CoreState.starting
        with patch(
            "custom_components.ssd_ims.coordinator.get_instance"
        ) as mock_get_instance:
            await coordinator._update_hourly_costs([POD_ID])
        mock_get_instance.assert_not_called()


class TestHourlyCostSinglePrice:
    async def _run(self, config):
        from homeassistant.core import CoreState

        coordinator = _make_coordinator(MagicMock(), config, data={})
        coordinator.hass.state = CoreState.running
        coordinator._registry_readings = {POD_ID: READINGS}
        coordinator._import_hourly_costs = AsyncMock()
        recorder = MagicMock()
        recorder.async_block_till_done = AsyncMock()
        with patch(
            "custom_components.ssd_ims.coordinator.get_instance",
            return_value=recorder,
        ):
            await coordinator._update_hourly_costs([POD_ID])
        return coordinator._import_hourly_costs

    async def test_vt_price_alone_prices_all_consumption(self):
        """Single-rate tariffs (D1/D2) only have one price."""
        import_costs = await self._run({CONF_PRICE_VT: 0.3})
        import_costs.assert_awaited_once()
        assert import_costs.await_args.args[-2:] == (0.3, 0.3)

    async def test_both_prices_are_used_as_given(self):
        import_costs = await self._run({CONF_PRICE_VT: 0.3, CONF_PRICE_NT: 0.1})
        assert import_costs.await_args.args[-2:] == (0.3, 0.1)

    async def test_nt_price_alone_imports_no_hourly_cost(self):
        import_costs = await self._run({CONF_PRICE_NT: 0.1})
        import_costs.assert_not_awaited()


class TestSupplierPrices:
    """Prices from the sse_ezona sensors matched by the POD's EIC."""

    def _coordinator(self, entities, states, config=None):
        """Coordinator with the given sse_ezona entities and their states."""
        coordinator = _make_coordinator(MagicMock(), config or {})
        del coordinator._supplier_prices  # use the real method
        registry = MagicMock()
        registry.async_get_entity_id.side_effect = lambda platform, domain, unique_id: (
            entities.get(unique_id)
            if (platform, domain) == ("sensor", "sse_ezona")
            else None
        )
        coordinator.hass.states.get.side_effect = lambda entity_id: (
            MagicMock(state=states[entity_id]) if entity_id in states else None
        )
        self._patch = patch(
            "custom_components.ssd_ims.coordinator.er.async_get",
            return_value=registry,
        )
        self._patch.start()
        return coordinator

    def teardown_method(self):
        """Stop the entity registry patch."""
        if hasattr(self, "_patch"):
            self._patch.stop()

    def test_two_rate_prices_override_the_options(self):
        """Supplier prices win over the options."""
        coordinator = self._coordinator(
            {
                f"ELECTRICITY_{POD_ID}_price_vt": "sensor.home_vt",
                f"ELECTRICITY_{POD_ID}_price_nt": "sensor.home_nt",
            },
            {"sensor.home_vt": "0.250211", "sensor.home_nt": "0.137649"},
            {CONF_PRICE_VT: 0.3, CONF_PRICE_NT: 0.2},
        )
        assert coordinator._pod_prices(POD_ID) == (0.250211, 0.137649)

    def test_single_rate_price_is_the_vt_price(self):
        """JT is the only price of a single-rate tariff."""
        coordinator = self._coordinator(
            {f"ELECTRICITY_{POD_ID}_price_jt": "sensor.home_jt"},
            {"sensor.home_jt": "0.2"},
        )
        assert coordinator._pod_prices(POD_ID) == (0.2, None)

    def test_without_supplier_sensors_the_options_are_used(self):
        """Sensors of another EIC don't count."""
        coordinator = self._coordinator(
            {"ELECTRICITY_OTHER_price_vt": "sensor.other_vt"},
            {"sensor.other_vt": "0.1"},
            {CONF_PRICE_VT: 0.3, CONF_PRICE_NT: 0.2},
        )
        assert coordinator._pod_prices(POD_ID) == (0.3, 0.2)

    def test_unavailable_supplier_price_is_no_price(self):
        """Don't fall back to a possibly stale manual price."""
        coordinator = self._coordinator(
            {
                f"ELECTRICITY_{POD_ID}_price_vt": "sensor.home_vt",
                f"ELECTRICITY_{POD_ID}_price_nt": "sensor.home_nt",
            },
            {"sensor.home_vt": "unavailable", "sensor.home_nt": "0.1"},
            {CONF_PRICE_VT: 0.3},
        )
        assert coordinator._pod_prices(POD_ID) == (None, None)

    def test_unavailable_nt_price_is_no_price(self):
        """Night consumption must not be priced at the VT price."""
        coordinator = self._coordinator(
            {
                f"ELECTRICITY_{POD_ID}_price_vt": "sensor.home_vt",
                f"ELECTRICITY_{POD_ID}_price_nt": "sensor.home_nt",
            },
            {"sensor.home_vt": "0.25"},
        )
        assert coordinator._pod_prices(POD_ID) == (None, None)

    async def test_supplier_prices_are_used_for_register_costs(self):
        """Monthly costs use the supplier prices too."""
        coordinator = self._coordinator(
            {
                f"ELECTRICITY_{POD_ID}_price_vt": "sensor.home_vt",
                f"ELECTRICITY_{POD_ID}_price_nt": "sensor.home_nt",
            },
            {"sensor.home_vt": "0.5", "sensor.home_nt": "0.1"},
        )
        coordinator.api_client.get_registry_data = AsyncMock(return_value=READINGS)

        _, mock_add_stats = await _run_registry_update(coordinator)

        imported = _imported(mock_add_stats)
        assert [s["sum"] for s in imported[f"ssd_ims:{POD_ID.lower()}_cost_vt"]] == [
            5.0,
            7.5,
        ]


class TestHourlyCostSupplierPrices:
    async def test_supplier_prices_are_used_without_options(self):
        """Hourly costs use the supplier prices without any options."""
        from homeassistant.core import CoreState

        coordinator = _make_coordinator(MagicMock(), {}, data={})
        coordinator._supplier_prices = MagicMock(return_value=(0.25, 0.14))
        coordinator.hass.state = CoreState.running
        coordinator._registry_readings = {POD_ID: READINGS}
        coordinator._import_hourly_costs = AsyncMock()
        recorder = MagicMock()
        recorder.async_block_till_done = AsyncMock()
        with patch(
            "custom_components.ssd_ims.coordinator.get_instance",
            return_value=recorder,
        ):
            await coordinator._update_hourly_costs([POD_ID])
        assert coordinator._import_hourly_costs.await_args.args[-2:] == (0.25, 0.14)


def _state_event(old, new):
    return MagicMock(
        data={
            "old_state": None if old is None else MagicMock(state=old),
            "new_state": None if new is None else MagicMock(state=new),
        }
    )


class TestSupplierPriceChanges:
    """A changed supplier price reprices without new portal requests."""

    def _coordinator(self):
        """Coordinator whose refresh requests are recorded."""
        coordinator = _make_coordinator(MagicMock(), {}, data={POD_ID: {}})
        coordinator.async_request_refresh = MagicMock()
        return coordinator

    @pytest.mark.parametrize(
        ("old", "new"), [(None, "0.25"), ("unavailable", "0.25"), ("0.25", "0.26")]
    )
    def test_new_numeric_price_requests_a_reprice(self, old, new):
        """A price that appears or changes requests a repricing refresh."""
        coordinator = self._coordinator()
        coordinator._async_supplier_price_changed(_state_event(old, new))
        assert coordinator._reprice_requested
        coordinator.hass.async_create_task.assert_called_once()

    @pytest.mark.parametrize(
        ("old", "new"), [("0.25", "0.25"), ("0.25", "unavailable"), ("0.25", None)]
    )
    def test_no_new_price_requests_nothing(self, old, new):
        """Unchanged or missing prices don't reprice."""
        coordinator = self._coordinator()
        coordinator._async_supplier_price_changed(_state_event(old, new))
        assert not coordinator._reprice_requested
        coordinator.hass.async_create_task.assert_not_called()

    async def test_reprice_runs_despite_the_daily_gate(self):
        """The gate skips portal requests, not the repricing."""
        coordinator = self._coordinator()
        coordinator._reprice_requested = True
        coordinator._async_reprice = AsyncMock()
        with patch(
            "custom_components.ssd_ims.coordinator.dt_util.now",
            return_value=datetime(2026, 11, 8, 12, tzinfo=UTC),
        ):
            coordinator._last_successful_data_date = date(2026, 11, 8)
            assert await coordinator._async_update_data() == {POD_ID: {}}
        coordinator._async_reprice.assert_awaited_once()
        coordinator.api_client.get_registry_data.assert_not_called()

    async def test_reprice_imports_register_costs_from_cached_readings(self):
        """Monthly costs skipped while the price was unknown get imported."""
        coordinator = self._coordinator()
        coordinator.config = {CONF_PRICE_VT: 0.5, CONF_PRICE_NT: 0.1}
        coordinator._registry_readings = {POD_ID: READINGS}
        coordinator._update_hourly_costs = AsyncMock()
        with (
            patch(
                "custom_components.ssd_ims.coordinator.get_instance"
            ) as mock_get_instance,
            patch(
                "custom_components.ssd_ims.coordinator.get_last_statistics",
                return_value={},
            ),
            patch(
                "custom_components.ssd_ims.coordinator.async_add_external_statistics"
            ) as mock_add_stats,
        ):
            mock_instance = MagicMock()
            mock_instance.async_add_executor_job = AsyncMock(side_effect=_run_executor)
            mock_get_instance.return_value = mock_instance
            await coordinator._async_reprice()

        imported = _imported(mock_add_stats)
        assert [s["sum"] for s in imported[f"ssd_ims:{POD_ID.lower()}_cost_vt"]] == [
            5.0,
            7.5,
        ]
        coordinator._update_hourly_costs.assert_awaited_once_with([POD_ID])
        assert not coordinator._reprice_requested
        coordinator.api_client.get_registry_data.assert_not_called()

    def test_watches_only_the_supplier_sensors_of_configured_pods(self):
        """Subscribes once per set of sensors, and resubscribes on change."""
        coordinator = self._coordinator()
        registry = MagicMock()
        registry.async_get_entity_id.side_effect = lambda platform, domain, uid: (
            "sensor.home_vt" if uid == f"ELECTRICITY_{POD_ID}_price_vt" else None
        )
        unsub = MagicMock()
        with (
            patch(
                "custom_components.ssd_ims.coordinator.er.async_get",
                return_value=registry,
            ),
            patch(
                "custom_components.ssd_ims.coordinator.async_track_state_change_event",
                return_value=unsub,
            ) as mock_track,
        ):
            coordinator._watch_supplier_prices([POD_ID])
            coordinator._watch_supplier_prices([POD_ID])
        mock_track.assert_called_once()
        assert mock_track.call_args.args[1] == ["sensor.home_vt"]

        coordinator.async_unwatch_supplier_prices()
        unsub.assert_called_once()
