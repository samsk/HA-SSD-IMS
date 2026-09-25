"""Test suite for SSD IMS API client."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientSession

from custom_components.ssd_ims.api_client import SsdImsApiClient


class TestSsdImsApiClient:
    """Test suite for SSD IMS API client."""

    @pytest.fixture
    async def api_client(self):
        """Create API client instance for testing."""
        session = ClientSession()
        client = SsdImsApiClient(session)
        yield client
        await session.close()

    @pytest.fixture
    def mock_auth_response(self):
        """Mock authentication response."""
        return {
            "userProfile": {
                "userId": 15492,
                "username": "test_user",
                "fullName": "Test User",
                "email": "test@example.com",
                "createdOn": "2022-03-02T14:24:11.3012130Z",
                "changedOn": "2025-08-20T09:16:06.9163210Z",
            },
            "userActions": [10001, 10002, 10003],
            "passwordExpirationDate": "2025-09-30T11:43:43.8579390Z",
            "showPasswordChangeWarning": False,
        }

    @pytest.fixture
    def mock_pods_response(self):
        """Mock PODs response."""
        return [{"text": "99XXX1234560000G (Rodinný dom)", "value": "test_pod_id"}]

    @pytest.fixture
    def mock_chart_response(self):
        """Mock chart data response."""
        return {
            "meteringDatetime": ["2025-01-20T10:15:00.0000000Z"],
            "actualConsumption": [0.1320],
            "actualSupply": [0.0],
            "idleConsumption": [0.0],
            "idleSupply": [0.72],
            "sumActualConsumption": 16.7000,
            "sumActualSupply": 18.7760,
            "sumIdleConsumption": 0.0,
            "sumIdleSupply": 42.7910,
        }

    class TestAuthentication:
        """Test authentication functionality."""

        async def test_successful_authentication(self, api_client, mock_auth_response):
            """Test successful login with valid credentials."""
            with patch.object(api_client._session, "post") as mock_post:
                mock_response = AsyncMock()
                mock_response.json = AsyncMock(return_value=mock_auth_response)
                mock_response.status = 200
                mock_response.headers = {"content-type": "application/json"}
                mock_response.cookies = {}
                mock_post.return_value.__aenter__.return_value = mock_response

                result = await api_client.authenticate("test_user", "test_pass")

                assert result is True
                assert api_client._authenticated is True
                mock_post.assert_called_once()

        async def test_invalid_credentials(self, api_client):
            """Test authentication with invalid credentials."""
            with patch.object(api_client._session, "post") as mock_post:
                mock_response = AsyncMock()
                mock_response.status = 401
                mock_response.headers = {"content-type": "application/json"}
                mock_response.cookies = {}
                mock_post.return_value.__aenter__.return_value = mock_response

                result = await api_client.authenticate("invalid", "invalid")

                assert result is False
                assert api_client._authenticated is False

        # Captured from the live portal (https://ims.ssd.sk/api/account/login)
        # with deliberately non-existent credentials — it answers a wrong
        # username/password with 422, never 401/403 (see issue #25).
        WRONG_CREDENTIALS_422_BODY = {
            "error": {
                "module": "AC",
                "code": "0x0100000F",
                "message": "Zadali ste nesprávne prihlasovacie meno alebo heslo.",
                "siId": "[00003;00031]-0000E812_3248FE14_F100E321",
                "traceId": "0HNOQK1C8VI8U:000010E6",
            }
        }
        # Same endpoint, empty username/password.
        MISSING_FIELDS_422_BODY = {
            "modelErrors": [
                {
                    "field": "Password",
                    "messages": ["The Password field is required."],
                },
                {
                    "field": "Username",
                    "messages": ["The Username field is required."],
                },
            ]
        }

        @pytest.mark.parametrize(
            "body",
            [WRONG_CREDENTIALS_422_BODY, MISSING_FIELDS_422_BODY],
            ids=["wrong_credentials", "missing_fields"],
        )
        async def test_422_during_login_is_invalid_credentials(
            self, api_client, body, caplog
        ):
            """A 422 from the login endpoint is how the portal rejects bad
            credentials. It must return False (so setup raises
            ConfigEntryAuthFailed and the reauth flow starts), not raise a
            generic RuntimeError (which setup treats as ConfigEntryNotReady
            and retries forever with the stale password)."""
            with patch.object(api_client._session, "post") as mock_post:
                mock_response = AsyncMock()
                mock_response.status = 422
                mock_response.json = AsyncMock(return_value=body)
                mock_response.headers = {"content-type": "application/json"}
                mock_response.cookies = {}
                mock_post.return_value.__aenter__.return_value = mock_response

                result = await api_client.authenticate("test_user", "wrong")

                assert result is False
                assert api_client._authenticated is False

            if "error" in body:
                assert "0x0100000F" in caplog.text
            else:
                assert "invalid request" in caplog.text

        async def test_422_with_unreadable_body_is_invalid_credentials(
            self, api_client
        ):
            """The error body is only read for logging — a body that can't be
            parsed must not turn a rejected login into an exception."""
            with patch.object(api_client._session, "post") as mock_post:
                mock_response = AsyncMock()
                mock_response.status = 422
                mock_response.json = AsyncMock(side_effect=ValueError("not json"))
                mock_response.headers = {"content-type": "text/plain"}
                mock_response.cookies = {}
                mock_post.return_value.__aenter__.return_value = mock_response

                result = await api_client.authenticate("test_user", "wrong")

                assert result is False

        async def test_422_on_session_reauth_raises_authentication_error(
            self, api_client, mock_auth_response
        ):
            """If the password is changed on the portal while HA is running,
            the transparent re-login after a session expiry gets a 422. That
            must surface as SsdImsAuthenticationError (which the coordinator
            maps to ConfigEntryAuthFailed), not a generic RuntimeError."""
            from custom_components.ssd_ims.api_client import (
                SsdImsAuthenticationError,
            )

            api_client._authenticated = True
            api_client._username = "test_user"
            api_client._password = "old_pass"

            with (
                patch.object(api_client._session, "request") as mock_request,
                patch.object(api_client._session, "post") as mock_post,
            ):
                expired = AsyncMock()
                expired.status = 401
                expired.headers = {"content-type": "application/json"}
                mock_request.return_value.__aenter__.return_value = expired

                rejected = AsyncMock()
                rejected.status = 422
                rejected.json = AsyncMock(return_value=self.WRONG_CREDENTIALS_422_BODY)
                rejected.headers = {"content-type": "application/json"}
                rejected.cookies = {}
                mock_post.return_value.__aenter__.return_value = rejected

                with pytest.raises(
                    SsdImsAuthenticationError, match="Re-authentication failed"
                ):
                    await api_client.get_points_of_delivery()

        async def test_server_error_during_login_raises_typed_server_error(
            self, api_client
        ):
            """A 5xx during login (e.g. the portal's own maintenance window)
            must raise the typed SsdImsServerError — the same classification
            used for authenticated requests — so it's clearly distinguished
            from a generic/unexpected response and still lets __init__.py's
            existing RuntimeError handling raise ConfigEntryNotReady."""
            from custom_components.ssd_ims.api_client import SsdImsServerError

            with patch.object(api_client._session, "post") as mock_post:
                mock_response = AsyncMock()
                mock_response.status = 503
                mock_response.headers = {"content-type": "application/json"}
                mock_response.cookies = {}
                mock_post.return_value.__aenter__.return_value = mock_response

                with pytest.raises(SsdImsServerError, match="503"):
                    await api_client.authenticate("test_user", "test_pass")

                assert api_client._authenticated is False

        async def test_network_error_during_auth_propagates(self, api_client):
            """Network errors during authentication must propagate rather than
            being reported as invalid credentials — callers (e.g. __init__.py)
            need to tell "wrong password" apart from "couldn't reach the
            portal" so they can raise ConfigEntryNotReady instead of
            ConfigEntryAuthFailed."""
            with patch.object(api_client._session, "post") as mock_post:
                mock_post.side_effect = Exception("Network error")

                with pytest.raises(Exception, match="Network error"):
                    await api_client.authenticate("test_user", "test_pass")

                assert api_client._authenticated is False

    class TestPointsOfDelivery:
        """Test POD discovery functionality."""

        async def test_successful_pod_discovery(self, api_client, mock_pods_response):
            """Test successful POD retrieval."""
            api_client._authenticated = True

            with patch.object(api_client._session, "request") as mock_request:
                mock_response = AsyncMock()
                mock_response.json = AsyncMock(return_value=mock_pods_response)
                mock_response.status = 200
                mock_response.headers = {"content-type": "application/json"}
                mock_request.return_value.__aenter__.return_value = mock_response

                pods = await api_client.get_points_of_delivery()

                assert len(pods) == 1
                assert pods[0].text == "99XXX1234560000G (Rodinný dom)"
                assert pods[0].value == "test_pod_id"

        async def test_pod_with_unparseable_text_is_skipped_not_fatal(self, api_client):
            """A single POD whose text can't be parsed into a stable ID
            (e.g. the portal changes its display format) must not take down
            discovery for every other, valid POD."""
            api_client._authenticated = True
            raw_pods = [
                {"text": "99XXX1234560000G (Rodinný dom)", "value": "good"},
                {"text": "not a valid pod identifier", "value": "bad"},
            ]

            with patch.object(api_client._session, "request") as mock_request:
                mock_response = AsyncMock()
                mock_response.json = AsyncMock(return_value=raw_pods)
                mock_response.status = 200
                mock_response.headers = {"content-type": "application/json"}
                mock_request.return_value.__aenter__.return_value = mock_response

                pods = await api_client.get_points_of_delivery()

                assert len(pods) == 1
                assert pods[0].value == "good"

        async def test_empty_pods_response(self, api_client):
            """Test handling of empty POD response."""
            api_client._authenticated = True

            with patch.object(api_client._session, "request") as mock_request:
                mock_response = AsyncMock()
                mock_response.json = AsyncMock(return_value=[])
                mock_response.status = 200
                mock_response.headers = {"content-type": "application/json"}
                mock_request.return_value.__aenter__.return_value = mock_response

                pods = await api_client.get_points_of_delivery()

                assert len(pods) == 0

        async def test_unauthorized_pod_request(self, api_client):
            """Test POD request without authentication."""
            from custom_components.ssd_ims.api_client import SsdImsAuthenticationError

            with pytest.raises(SsdImsAuthenticationError):
                await api_client.get_points_of_delivery()

    class TestChartData:
        """Test chart data retrieval."""

        async def test_successful_chart_retrieval(
            self, api_client, mock_chart_response
        ):
            """Test successful chart data retrieval."""
            api_client._authenticated = True
            pod_id = "99XXX1234560000G"
            from_date = datetime(2025, 1, 20, 0, 0)
            to_date = datetime(2025, 1, 20, 23, 59)

            pod_mock = MagicMock()
            pod_mock.id = pod_id
            pod_mock.value = "test_pod_id"
            pod_mock.text = "99XXX1234560000G (Rodinný dom)"
            api_client._pods_cache = [pod_mock]
            api_client._pods_cache_ts = datetime.now(UTC)

            with patch.object(api_client._session, "request") as mock_request:
                mock_response = AsyncMock()
                mock_response.json = AsyncMock(return_value=mock_chart_response)
                mock_response.status = 200
                mock_response.headers = {"content-type": "application/json"}
                mock_request.return_value.__aenter__.return_value = mock_response

                chart_data = await api_client.get_chart_data(pod_id, from_date, to_date)

                assert chart_data.sum_actual_consumption == 16.7000
                assert chart_data.sum_actual_supply == 18.7760
                assert len(chart_data.metering_datetime) == 1
                assert len(chart_data.actual_consumption) == 1

        async def test_chart_data_with_none_values(self, api_client):
            """Test chart data handling with None values in arrays."""
            api_client._authenticated = True
            pod_id = "99XXX1234560000G"
            from_date = datetime(2025, 1, 20, 0, 0)
            to_date = datetime(2025, 1, 20, 23, 59)

            pod_mock = MagicMock()
            pod_mock.id = pod_id
            pod_mock.value = "test_pod_id"
            pod_mock.text = "99XXX1234560000G (Rodinný dom)"
            api_client._pods_cache = [pod_mock]
            api_client._pods_cache_ts = datetime.now(UTC)

            response_with_nones = {
                "meteringDatetime": [
                    "2025-01-20T10:15:00.0000000Z",
                    "2025-01-20T10:30:00.0000000Z",
                ],
                "actualConsumption": [0.1320, None],
                "actualSupply": [None, 0.5],
                "idleConsumption": [0.0, 0.0],
                "idleSupply": [0.72, 0.0],
                "sumActualConsumption": 16.7000,
                "sumActualSupply": 18.7760,
                "sumIdleConsumption": 0.0,
                "sumIdleSupply": 42.7910,
            }

            with patch.object(api_client._session, "request") as mock_request:
                mock_response = AsyncMock()
                mock_response.json = AsyncMock(return_value=response_with_nones)
                mock_response.status = 200
                mock_response.headers = {"content-type": "application/json"}
                mock_request.return_value.__aenter__.return_value = mock_response

                chart_data = await api_client.get_chart_data(pod_id, from_date, to_date)

                # None entries must be zero-filled, not dropped: the list
                # length has to stay aligned with metering_datetime since
                # coordinator.py indexes these lists positionally.
                assert len(chart_data.actual_consumption) == len(
                    chart_data.metering_datetime
                )
                assert chart_data.actual_consumption[0] == 0.1320
                assert chart_data.actual_consumption[1] == 0.0
                assert len(chart_data.actual_supply) == len(
                    chart_data.metering_datetime
                )
                assert chart_data.actual_supply[0] == 0.0
                assert chart_data.actual_supply[1] == 0.5

    class TestErrorHandling:
        """Test error handling scenarios."""

        async def test_session_timeout(self, api_client):
            """Test handling of session timeouts."""
            api_client._authenticated = True

            with patch.object(api_client._session, "request") as mock_request:
                mock_response = AsyncMock()
                mock_response.status = 408
                mock_response.headers = {"content-type": "application/json"}
                mock_request.return_value.__aenter__.return_value = mock_response

                with pytest.raises(RuntimeError, match="408"):
                    await api_client.get_points_of_delivery()

        async def test_rate_limiting(self, api_client):
            """Test handling of rate limiting responses."""
            api_client._authenticated = True

            with patch.object(api_client._session, "request") as mock_request:
                mock_response = AsyncMock()
                mock_response.status = 429
                mock_response.headers = {"content-type": "application/json"}
                mock_request.return_value.__aenter__.return_value = mock_response

                with pytest.raises(RuntimeError, match="429"):
                    await api_client.get_points_of_delivery()

        async def test_server_error_is_retried_then_raised(self, api_client):
            """5xx responses are transient and must be retried (unlike other
            4xx errors), and raise the typed SsdImsServerError so callers can
            tell it apart from an authentication problem."""
            from custom_components.ssd_ims.api_client import SsdImsServerError

            api_client._authenticated = True

            with (
                patch.object(api_client._session, "request") as mock_request,
                patch(
                    "custom_components.ssd_ims.api_client.asyncio.sleep",
                    AsyncMock(),
                ),
            ):
                mock_response = AsyncMock()
                mock_response.status = 500
                mock_response.headers = {"content-type": "application/json"}
                mock_request.return_value.__aenter__.return_value = mock_response

                with pytest.raises(SsdImsServerError):
                    await api_client.get_points_of_delivery()

                assert mock_request.call_count == 3  # default max_retries

        async def test_server_error_succeeds_after_transient_retry(
            self, api_client, mock_pods_response
        ):
            """A 500 followed by a successful response must not be treated as
            a permanent failure."""
            api_client._authenticated = True

            with (
                patch.object(api_client._session, "request") as mock_request,
                patch(
                    "custom_components.ssd_ims.api_client.asyncio.sleep",
                    AsyncMock(),
                ),
            ):
                error_response = AsyncMock()
                error_response.status = 500
                error_response.headers = {"content-type": "application/json"}

                ok_response = AsyncMock()
                ok_response.status = 200
                ok_response.headers = {"content-type": "application/json"}
                ok_response.json = AsyncMock(return_value=mock_pods_response)

                mock_request.return_value.__aenter__.side_effect = [
                    error_response,
                    ok_response,
                ]

                pods = await api_client.get_points_of_delivery()

                assert len(pods) == 1
                assert mock_request.call_count == 2

    class TestSessionManagement:
        """Test session management functionality."""

        async def test_session_expiration_detection(self, api_client):
            """Test detection of session expiration via HTML content type."""
            mock_response = MagicMock()
            mock_response.status = 200
            mock_response.headers = {"content-type": "text/html; charset=utf-8"}

            result = api_client._is_session_expired(mock_response)
            assert result is True

        async def test_session_expiration_detection_401(self, api_client):
            """Test detection of session expiration via 401 status."""
            mock_response = MagicMock()
            mock_response.status = 401
            mock_response.headers = {"content-type": "application/json"}

            result = api_client._is_session_expired(mock_response)
            assert result is True

        async def test_session_not_expired(self, api_client):
            """Test detection when session is still valid."""
            mock_response = MagicMock()
            mock_response.status = 200
            mock_response.headers = {"content-type": "application/json"}

            result = api_client._is_session_expired(mock_response)
            assert result is False

        async def test_reauthentication_with_stored_credentials(
            self, api_client, mock_auth_response
        ):
            """Test re-authentication with stored credentials."""
            api_client._username = "test_user"
            api_client._password = "test_pass"

            with patch.object(api_client, "authenticate") as mock_auth:
                mock_auth.return_value = True

                result = await api_client._reauthenticate()

                assert result is True
                mock_auth.assert_called_once_with("test_user", "test_pass")

        async def test_reauthentication_without_credentials(self, api_client):
            """Test re-authentication without stored credentials."""
            result = await api_client._reauthenticate()
            assert result is False


class TestPodIdExtraction:
    """Test POD ID extraction from text."""

    @pytest.fixture
    async def api_client(self):
        """Create API client instance for testing."""
        session = ClientSession()
        client = SsdImsApiClient(session)
        yield client
        await session.close()

    async def test_pod_id_extraction(self, api_client):
        """Test that POD ID is correctly extracted from pod.text."""
        api_client._authenticated = True

        mock_response_data = [
            {"text": "99XXX1234560000G (Rodinný dom)", "value": "session_token_123"},
            {"text": "99YYY9876540000G (Garáž)", "value": "session_token_456"},
        ]

        with patch.object(api_client._session, "request") as mock_request:
            mock_response = AsyncMock()
            mock_response.json = AsyncMock(return_value=mock_response_data)
            mock_response.status = 200
            mock_response.headers = {"content-type": "application/json"}
            mock_request.return_value.__aenter__.return_value = mock_response

            pods = await api_client.get_points_of_delivery()

            assert len(pods) == 2
            assert pods[0].id == "99XXX1234560000G"
            assert pods[1].id == "99YYY9876540000G"


class TestSsdImsSensor:
    """Test suite for SSD IMS sensor entities."""

    def test_ssd_ims_sensors_enabled_by_default_for_all_sensor_types(self):
        """Ensure SSD IMS sensors are enabled by default for all sensor types."""
        from custom_components.ssd_ims.sensor import SsdImsYesterdaySensor

        mock_coordinator = MagicMock()
        mock_coordinator.data = {
            "pod_id_123": {
                "aggregated_data": {"actual_consumption": 10.5, "actual_supply": 2.3}
            }
        }

        pod_id = "pod_id_123"
        sensor_types = ("actual_consumption", "actual_supply")

        for sensor_type in sensor_types:
            sensor = SsdImsYesterdaySensor(
                coordinator=mock_coordinator,
                sensor_type=sensor_type,
                pod_id=pod_id,
                friendly_name="Home",
            )

            assert sensor.entity_registry_enabled_default is True
            assert (
                sensor.native_value
                == mock_coordinator.data[pod_id]["aggregated_data"][sensor_type]
            )

    def test_yesterday_sensor_has_no_state_class(self):
        """The yesterday sensor is a daily snapshot that can legitimately
        decrease day-to-day, not a running total. TOTAL_INCREASING/TOTAL
        would make HA auto-generate a second, redundant long-term
        statistics series alongside the one the coordinator writes
        explicitly, and MEASUREMENT isn't a legal alternative for the
        ENERGY device class (HA logs an "impossible" warning and rejects
        it) — so state_class must be left unset entirely."""
        from custom_components.ssd_ims.sensor import SsdImsYesterdaySensor

        sensor = SsdImsYesterdaySensor(
            coordinator=MagicMock(),
            sensor_type="actual_consumption",
            pod_id="pod_id_123",
            friendly_name="Home",
        )

        assert sensor.state_class is None

    def test_cumulative_sensor_is_total_increasing(self):
        """The cumulative sensor mirrors an ever-increasing running total
        read back from statistics, so TOTAL_INCREASING is correct here."""
        from homeassistant.components.sensor import SensorStateClass

        from custom_components.ssd_ims.sensor import SsdImsCumulativeSensor

        sensor = SsdImsCumulativeSensor(
            coordinator=MagicMock(),
            sensor_type="actual_consumption",
            pod_id="pod_id_123",
            friendly_name="Home",
        )

        assert sensor.state_class == SensorStateClass.TOTAL_INCREASING
