"""API client for SSD IMS integration."""
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from aiohttp import ClientError, ClientSession, ClientTimeout
from pydantic import ValidationError

from .const import API_CHART, API_DATA, API_LOGIN, API_PODS
from .models import (AuthResponse, ChartData, MeteringData,
                     MeteringDataResponse, PointOfDelivery)

_LOGGER = logging.getLogger(__name__)


def _log_data_sample(data: Dict[str, Any], field_name: str, max_sample_size: int = 20) -> str:
    """Create a debug-friendly sample of problematic data."""
    if field_name not in data:
        return f"Field '{field_name}' not found in data"
    
    field_data = data[field_name]
    if not isinstance(field_data, list):
        return f"Field '{field_name}' is not a list: {type(field_data).__name__} = {repr(field_data)}"
    
    total_len = len(field_data)
    if total_len == 0:
        return f"Field '{field_name}' is empty list"
    
    # Find problematic entries
    problems = []
    for i, val in enumerate(field_data):
        if val is None or (isinstance(val, str) and val.strip() == ""):
            problems.append(i)
        elif not isinstance(val, (int, float, str)):
            problems.append(i)
    
    sample_info = f"length={total_len}"
    if problems:
        sample_info += f", problems_at={problems[:10]}"
        if len(problems) > 10:
            sample_info += f"+{len(problems)-10}more"
    
    # Show a small sample around problematic areas
    if problems:
        sample_ranges = []
        for prob_idx in problems[:3]:  # Show first 3 problem areas
            start = max(0, prob_idx - 2)
            end = min(total_len, prob_idx + 3)
            sample_ranges.append(f"[{start}:{end}]={field_data[start:end]}")
        sample_info += f", samples={'; '.join(sample_ranges)}"
    else:
        # Show beginning sample if no obvious problems
        sample_size = min(max_sample_size, total_len)
        sample_info += f", sample={field_data[:sample_size]}"
        if total_len > sample_size:
            sample_info += "..."
    
    return sample_info


class SsdImsApiClient:
    """API client for SSD IMS portal."""

    def __init__(self, session: ClientSession) -> None:
        """Initialize API client."""
        self._session = session
        self._authenticated = False
        self._session_token: Optional[str] = None
        self._timeout = ClientTimeout(total=30)
        self._username: Optional[str] = None
        self._password: Optional[str] = None

    async def authenticate(self, username: str, password: str) -> bool:
        """Authenticate with SSD IMS portal."""
        try:
            # Store credentials for re-authentication
            self._username = username
            self._password = password

            payload = {"username": username, "password": password}

            async with self._session.post(
                API_LOGIN, json=payload, timeout=self._timeout
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    AuthResponse(**data)  # Validate response structure
                    self._authenticated = True

                    # Extract session token from cookies
                    self._session_token = self._extract_session_token(response)
                    if self._session_token:
                        _LOGGER.debug(
                            "Session token extracted: %s",
                            self._session_token[:20] + "...",
                        )
                    else:
                        _LOGGER.warning("No session token found in response cookies")

                    _LOGGER.info("Authentication successful for user: %s", username)
                    return True
                else:
                    _LOGGER.error("Authentication failed: %s", response.status)
                    return False

        except ClientError as e:
            _LOGGER.error("Network error during authentication: %s", e)
            return False
        except Exception as e:
            _LOGGER.error("Unexpected error during authentication: %s", e)
            return False

    def _extract_session_token(self, response) -> Optional[str]:
        """Extract SsdAccessToken from response cookies."""
        try:
            cookies = response.cookies
            # aiohttp returns cookies as a SimpleCookie object
            if hasattr(cookies, "get"):
                # Try to get the SsdAccessToken cookie directly
                ssd_token_cookie = cookies.get("SsdAccessToken")
                if ssd_token_cookie:
                    return ssd_token_cookie.value
            return None
        except Exception as e:
            _LOGGER.error("Error extracting session token: %s", e)
            return None

    def _is_session_expired(self, response) -> bool:
        """Check if session has expired by examining response content type."""
        try:
            content_type = response.headers.get("content-type", "").lower()
            # Check if response is HTML (session expired) instead of JSON
            if "text/html" in content_type:
                _LOGGER.warning(
                    "Session expired - received HTML response instead of JSON"
                )
                return True
            return False
        except Exception as e:
            _LOGGER.error("Error checking session expiration: %s", e)
            return False

    async def _reauthenticate(self) -> bool:
        """Re-authenticate with stored credentials."""
        if not self._username or not self._password:
            _LOGGER.error("Cannot re-authenticate: no stored credentials")
            return False

        _LOGGER.info("Re-authenticating with SSD IMS...")
        self._authenticated = False
        self._session_token = None

        return await self.authenticate(self._username, self._password)

    async def _make_authenticated_request(
        self, method: str, url: str, **kwargs
    ) -> Any:
        """Make an authenticated request with automatic re-authentication on session expiry."""
        if not self._authenticated:
            raise Exception("Not authenticated")

        try:
            async with self._session.request(
                method, url, timeout=self._timeout, **kwargs
            ) as response:
                # Check if session has expired
                if self._is_session_expired(response):
                    _LOGGER.info("Session expired, attempting re-authentication...")
                    if await self._reauthenticate():
                        # Retry the request after re-authentication
                        _LOGGER.info(
                            "Re-authentication successful, retrying request..."
                        )
                        async with self._session.request(
                            method, url, timeout=self._timeout, **kwargs
                        ) as retry_response:
                            if retry_response.status == 200:
                                return await retry_response.json()
                            else:
                                raise Exception(
                                    f"API error after re-authentication: "
                                    f"{retry_response.status}"
                                )
                    else:
                        raise Exception("Re-authentication failed")

                if response.status == 200:
                    return await response.json()
                else:
                    raise Exception(f"API error: {response.status}")

        except ClientError as e:
            _LOGGER.error("Network error in authenticated request: %s", e)
            raise
        except Exception as e:
            _LOGGER.error("Unexpected error in authenticated request: %s", e)
            raise

    async def get_points_of_delivery(self) -> List[PointOfDelivery]:
        """Get available points of delivery."""
        if not self._authenticated:
            raise Exception("Not authenticated")

        try:
            data = await self._make_authenticated_request("GET", API_PODS)
            pods = [PointOfDelivery(**pod) for pod in data]
            _LOGGER.debug("Retrieved %d points of delivery", len(pods))
            return pods

        except ClientError as e:
            _LOGGER.error("Network error getting PODs: %s", e)
            raise
        except Exception as e:
            _LOGGER.error("Unexpected error getting PODs: %s", e)
            raise

    async def get_metering_data(
        self,
        pod_id: str,  # Use stable pod_id instead of pod_text for stable
        # identification
        from_date: datetime,
        to_date: datetime,
        page: int = 1,
        page_size: int = 100,
    ) -> List[MeteringData]:
        """Get detailed metering data for time period."""
        if not self._authenticated:
            raise Exception("Not authenticated")

        try:
            # First, get current session POD ID for this stable pod_id
            session_pod_id = await self._get_session_pod_id_by_stable_id(pod_id)
            if not session_pod_id:
                raise Exception(f"POD not found for stable ID: {pod_id}")

            payload = {
                "page": {"totalRows": 96, "currentPage": page, "pageSize": page_size},
                "filters": [
                    {
                        "member": "pointOfDeliveryId",
                        "operator": "Equals",
                        "type": "Int",
                        "value": session_pod_id,
                    },
                    {
                        "member": "meteringDatetime",
                        "operator": "Greater",
                        "type": "DateTimeMilliseconds",
                        "value": from_date.isoformat(),
                        "rangeOperator": "LowerOrEquals",
                        "rangeValue": to_date.isoformat(),
                    },
                ],
                "sort": [{"member": "meteringDatetime", "sortOrder": "asc"}],
                "isExport": False,
            }

            data = await self._make_authenticated_request(
                "POST", API_DATA, json=payload
            )
            response_model = MeteringDataResponse(**data)

            metering_data = []
            for row in response_model.rows:
                values = row.values
                if len(values) >= 10:
                    metering_data.append(
                        MeteringData(
                            metering_datetime=datetime.fromisoformat(
                                values[0].replace("Z", "+00:00")
                            ),
                            period=values[1],
                            actual_consumption=values[2]
                            if values[2] is not None
                            else None,
                            actual_supply=values[4] if values[4] is not None else None,
                            idle_consumption=values[6]
                            if values[6] is not None
                            else None,
                            idle_supply=values[8] if values[8] is not None else None,
                        )
                    )

            _LOGGER.debug(
                "Retrieved %d metering data points for POD %s",
                len(metering_data),
                pod_id,
            )
            return metering_data

        except ClientError as e:
            _LOGGER.error("Network error getting metering data: %s", e)
            raise
        except Exception as e:
            _LOGGER.error("Unexpected error getting metering data: %s", e)
            raise

    async def get_chart_data(
        self,
        pod_id: str,  # Use stable pod_id instead of pod_text for stable identification
        from_date: datetime,
        to_date: datetime,
    ) -> ChartData:
        """Get summary chart data for time period."""
        if not self._authenticated:
            raise Exception("Not authenticated")

        try:
            # First, get current session POD ID for this stable pod_id
            session_pod_id = await self._get_session_pod_id_by_stable_id(pod_id)
            if not session_pod_id:
                raise Exception(f"POD not found for stable ID: {pod_id}")

            # Get pod text for API call
            pod_text = await self._get_pod_text_by_stable_id(pod_id)
            if not pod_text:
                raise Exception(f"POD text not found for stable ID: {pod_id}")

            payload = {
                "pointOfDeliveryId": session_pod_id,
                "validFromDate": from_date.isoformat(),
                "validToDate": to_date.isoformat(),
                "pointOfDeliveryText": pod_text,
            }

            data = await self._make_authenticated_request(
                "POST", API_CHART, json=payload
            )
            _LOGGER.debug(
                "Chart data response keys: %s",
                list(data.keys()) if isinstance(data, dict) else "Not a dict",
            )

            # Validate that we have the expected data structure
            if not isinstance(data, dict):
                _LOGGER.error("Chart data response is not a dictionary: %s", type(data))
                raise Exception("Invalid chart data response format")

            # Check if we have any data
            if not data.get("meteringDatetime"):
                _LOGGER.warning(
                    "No metering data found for POD %s in period %s to %s",
                    pod_id,
                    from_date,
                    to_date,
                )
                # Return empty chart data
                return ChartData()

            # Enhanced validation with detailed error context
            try:
                chart_data = ChartData(**data)
                _LOGGER.debug(
                    "Retrieved chart data for POD %s, period %s to %s",
                    pod_id,
                    from_date,
                    to_date,
                )
                return chart_data
            except ValidationError as e:
                _LOGGER.error(
                    "Chart data validation failed for POD %s (%s to %s). "
                    "Raw API response structure: %s. "
                    "Validation errors: %s",
                    pod_id,
                    from_date,
                    to_date,
                    {k: f"{type(v).__name__}[{len(v) if isinstance(v, list) else 'scalar'}]" 
                     for k, v in data.items() if k in ['meteringDatetime', 'actualConsumption', 
                                                       'actualSupply', 'idleConsumption', 'idleSupply']},
                    str(e)
                )
                # Log detailed information about problematic data
                _LOGGER.error("Raw chart data field analysis:")
                for field in ['actualConsumption', 'actualSupply', 'idleConsumption', 'idleSupply']:
                    sample_info = _log_data_sample(data, field)
                    _LOGGER.error("  %s: %s", field, sample_info)
                raise Exception(f"Chart data validation failed: {str(e)}") from e

        except ClientError as e:
            _LOGGER.error("Network error getting chart data: %s", e)
            raise
        except Exception as e:
            _LOGGER.error("Unexpected error getting chart data: %s", e)
            raise

    async def _get_session_pod_id_by_stable_id(self, pod_id: str) -> Optional[str]:
        """Get current session POD ID for a given stable pod_id."""
        try:
            pods = await self.get_points_of_delivery()
            for pod in pods:
                try:
                    if pod.id == pod_id:
                        return pod.value
                except ValueError:
                    # Skip pods with invalid ID format
                    continue
            return None
        except Exception as e:
            _LOGGER.error(
                "Error getting session POD ID for stable ID %s: %s", pod_id, e
            )
            return None

    async def _get_pod_text_by_stable_id(self, pod_id: str) -> Optional[str]:
        """Get POD text for a given stable pod_id."""
        try:
            pods = await self.get_points_of_delivery()
            for pod in pods:
                try:
                    if pod.id == pod_id:
                        return pod.text
                except ValueError:
                    # Skip pods with invalid ID format
                    continue
            return None
        except Exception as e:
            _LOGGER.error("Error getting POD text for stable ID %s: %s", pod_id, e)
            return None

    async def _get_pod_id_by_text(self, pod_text: str) -> Optional[str]:
        """Get current POD ID for a given pod_text (label)."""
        try:
            pods = await self.get_points_of_delivery()
            for pod in pods:
                if pod.text == pod_text:
                    return pod.value
            return None
        except Exception as e:
            _LOGGER.error("Error getting POD ID for text %s: %s", pod_text, e)
            return None

    @property
    def is_authenticated(self) -> bool:
        """Check if client is authenticated."""
        return self._authenticated

    @property
    def session_token(self) -> Optional[str]:
        """Get current session token."""
        return self._session_token

    def logout(self) -> None:
        """Logout and clear authentication state."""
        self._authenticated = False
        self._session_token = None
        self._username = None
        self._password = None
        _LOGGER.debug("Logged out from SSD IMS")
