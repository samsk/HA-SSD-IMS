"""API client for SSD IMS integration."""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout
from pydantic import ValidationError

from .const import API_CHART, API_LOGIN, API_PODS, PODS_CACHE_TTL
from .models import AuthResponse, ChartData, PointOfDelivery

_LOGGER = logging.getLogger(__name__)


class SsdImsAuthenticationError(RuntimeError):
    """Raised when a request fails because the session/credentials are no
    longer valid, as opposed to a network or server-side problem.

    Callers can catch this specifically to distinguish "you need to log in
    again" from transient failures that should just be retried.
    """


class SsdImsServerError(RuntimeError):
    """Raised for a 5xx response from the portal — treated as retryable."""


def _log_data_sample(
    data: dict[str, Any], field_name: str, max_sample_size: int = 20
) -> str:
    """Create a debug-friendly sample of problematic data."""
    if field_name not in data:
        return f"Field '{field_name}' not found in data"

    field_data = data[field_name]
    if not isinstance(field_data, list):
        return f"Field '{field_name}' is not a list: {type(field_data).__name__} = {repr(field_data)}"

    total_len = len(field_data)
    if total_len == 0:
        return f"Field '{field_name}' is empty list"

    # Find problematic entries (None values are now valid for supply fields)
    problems = []
    for i, val in enumerate(field_data):
        if val is None:
            continue  # None is now valid - skip it
        elif isinstance(val, str) and val.strip() == "":
            problems.append(i)
        elif not isinstance(val, (int, float, str)):
            problems.append(i)

    sample_info = f"length={total_len}"
    # Show a small sample around problematic areas
    if problems:
        sample_info += f", problems_at={problems[:10]}"
        if len(problems) > 10:
            sample_info += f"+{len(problems) - 10}more"
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
        self._session_token: str | None = None
        self._timeout = ClientTimeout(total=60)  # Increase timeout for slow API
        self._username: str | None = None
        self._password: str | None = None
        self._pods_cache: list[PointOfDelivery] | None = None
        self._pods_cache_ts: datetime | None = None

    async def authenticate(self, username: str, password: str) -> bool:
        """Authenticate with SSD IMS portal.

        Returns False only when the portal itself rejects the credentials
        (401/403/422 — the portal actually answers a wrong username or
        password with 422, not 401). Network errors, timeouts, and unexpected/invalid
        responses are raised instead of being folded into False, so callers
        can tell "wrong password" apart from "couldn't reach the portal"
        (e.g. ConfigEntryAuthFailed vs. ConfigEntryNotReady).
        """
        # Store credentials for re-authentication
        self._username = username
        self._password = password

        payload = {"username": username, "password": password}

        async with self._session.post(
            API_LOGIN, json=payload, timeout=self._timeout
        ) as response:
            if response.status in (401, 403):
                _LOGGER.error("Authentication failed: %s", response.status)
                return False

            if response.status == 422:
                # The portal rejects a wrong username/password with 422 and an
                # {"error": {"code": ..., "message": ...}} body, and missing
                # fields with 422 and a {"modelErrors": [...]} body — both
                # mean the credentials we sent are unusable.
                _LOGGER.error(
                    "Authentication failed: 422 %s",
                    await self._read_login_error(response),
                )
                return False

            if response.status != 200:
                # 401/403/422 are already handled above; anything else reuses the
                # same typed classification as authenticated requests (in
                # particular, 5xx becomes SsdImsServerError instead of a
                # generic "unexpected" RuntimeError — a 503 during the
                # portal's own maintenance windows is a known, expected
                # condition, not something surprising).
                self._raise_for_status(response.status, " during login")

            data = await response.json()
            AuthResponse(**data)  # Validate response structure
            self._authenticated = True

            # Extract session token from cookies
            self._session_token = self._extract_session_token(response)
            if self._session_token:
                _LOGGER.debug(
                    "Session token extracted (length=%d)", len(self._session_token)
                )
            else:
                _LOGGER.warning("No session token found in response cookies")

            _LOGGER.info("Authentication successful for user: %s", username)
            return True

    @staticmethod
    async def _read_login_error(response) -> str:
        """Best-effort summary of a rejected login's error body, for logging."""
        try:
            data = await response.json(content_type=None)
        except Exception:
            return "(unreadable response body)"
        if isinstance(data, dict):
            if isinstance(error := data.get("error"), dict):
                return f"{error.get('code')}: {error.get('message')}"
            if model_errors := data.get("modelErrors"):
                return f"invalid request: {model_errors}"
        return str(data)

    def _extract_session_token(self, response) -> str | None:
        """Extract SsdAccessToken from response cookies."""
        try:
            cookies = response.cookies
            # aiohttp returns cookies as a SimpleCookie object
            if hasattr(cookies, "get") and (
                ssd_token_cookie := cookies.get("SsdAccessToken")
            ):
                return ssd_token_cookie.value
            return None
        except Exception as e:
            _LOGGER.error("Error extracting session token: %s", e)
            return None

    def _is_session_expired(self, response) -> bool:
        """Check if session has expired by examining response content type and status."""
        try:
            # Check for 401 unauthorized status
            if response.status == 401:
                _LOGGER.debug("Session expired - 401 unauthorized")
                return True

            content_type = response.headers.get("content-type", "").lower()
            # Check if response is HTML (session expired) instead of JSON.
            # This covers both non-200 redirects to a login page and rare cases
            # where the portal returns HTTP 200 with an HTML login page body.
            if "text/html" in content_type:
                _LOGGER.warning(
                    "Session expired - received HTML response (status=%s) instead of JSON",
                    response.status,
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

    async def _retry_request_with_backoff(
        self, method: str, url: str, max_retries: int = 3, **kwargs
    ) -> Any:
        """Retry request with exponential backoff for network issues."""
        for attempt in range(max_retries):
            try:
                return await self._make_authenticated_request(method, url, **kwargs)
            except (ClientError, SsdImsServerError) as e:
                if attempt == max_retries - 1:
                    raise

                wait_time = 2**attempt  # exponential backoff: 1s, 2s, 4s
                _LOGGER.warning(
                    "Transient error on attempt %d/%d for %s: %s. Retrying in %ds...",
                    attempt + 1,
                    max_retries,
                    url,
                    e,
                    wait_time,
                )
                await asyncio.sleep(wait_time)
            except Exception:
                # Don't retry auth failures or other non-transient errors
                raise

    @staticmethod
    def _raise_for_status(status: int, context: str = "") -> None:
        """Raise a typed exception for a non-200 response status."""
        if status == 401:
            raise SsdImsAuthenticationError(f"Authentication required{context}")
        if status == 403:
            raise RuntimeError(f"Access forbidden - check permissions{context}")
        if status == 404:
            raise RuntimeError(f"API endpoint not found{context}")
        if status >= 500:
            raise SsdImsServerError(f"Server error: {status}{context}")
        raise RuntimeError(f"API error: {status}{context}")

    async def _make_authenticated_request(self, method: str, url: str, **kwargs) -> Any:
        """Make an authenticated request with automatic re-authentication on session expiry."""
        if not self._authenticated:
            raise SsdImsAuthenticationError("Not authenticated")

        # Add required headers for SSD IMS API compatibility
        headers = dict(kwargs.get("headers", {}))
        headers["X-Requested-With"] = "XMLHttpRequest"
        # Add accept header to ensure JSON response
        headers["Accept"] = "application/json, text/plain, */*"
        kwargs["headers"] = headers

        async with self._session.request(
            method, url, timeout=self._timeout, **kwargs
        ) as response:
            # Check if session has expired
            if self._is_session_expired(response):
                _LOGGER.info("Session expired, attempting re-authentication...")
                if await self._reauthenticate():
                    # Retry the request after re-authentication
                    _LOGGER.info("Re-authentication successful, retrying request...")
                    async with self._session.request(
                        method, url, timeout=self._timeout, **kwargs
                    ) as retry_response:
                        if retry_response.status == 200:
                            return await retry_response.json()
                        self._raise_for_status(
                            retry_response.status, " (after re-authentication)"
                        )
                else:
                    raise SsdImsAuthenticationError("Re-authentication failed")

            if response.status == 200:
                return await response.json()
            self._raise_for_status(response.status)

    async def get_points_of_delivery(self) -> list[PointOfDelivery]:
        """Get available points of delivery."""
        if not self._authenticated:
            raise SsdImsAuthenticationError("Not authenticated")

        _LOGGER.debug("Fetching points of delivery from API")
        data = await self._retry_request_with_backoff("GET", API_PODS)
        _LOGGER.debug(
            "POD API response type: %s, length: %s",
            type(data).__name__,
            len(data) if isinstance(data, list) else "N/A",
        )
        pods = []
        for raw_pod in data:
            try:
                pods.append(PointOfDelivery(**raw_pod))
            except ValidationError as e:
                # A single POD with unparseable text (e.g. the portal
                # changes its display format) must not take down discovery
                # for every other, valid POD.
                _LOGGER.warning("Skipping POD with invalid data: %s - %s", raw_pod, e)

        self._pods_cache = pods
        self._pods_cache_ts = datetime.now(UTC)
        _LOGGER.debug("Retrieved %d points of delivery", len(pods))
        return pods

    async def get_chart_data(
        self,
        pod_id: str,  # Use stable pod_id instead of pod_text for stable identification
        from_date: datetime,
        to_date: datetime,
    ) -> ChartData:
        """Get summary chart data for time period."""
        if not self._authenticated:
            raise SsdImsAuthenticationError("Not authenticated")

        # Efficiently get session_pod_id and pod_text in one go
        target_pod = await self._get_pod_by_stable_id(pod_id)
        if not target_pod:
            raise RuntimeError(f"POD not found for stable ID: {pod_id}")

        session_pod_id = target_pod.value
        pod_text = target_pod.text

        payload = {
            "pointOfDeliveryId": session_pod_id,
            "validFromDate": from_date.isoformat(),
            "validToDate": to_date.isoformat(),
            "pointOfDeliveryText": pod_text,
        }

        _LOGGER.debug(
            "Chart data request: validFromDate=%s, validToDate=%s",
            payload["validFromDate"],
            payload["validToDate"],
        )

        data = await self._retry_request_with_backoff("POST", API_CHART, json=payload)
        _LOGGER.debug(
            "Chart data response keys: %s",
            list(data.keys()) if isinstance(data, dict) else "Not a dict",
        )

        # Validate that we have the expected data structure
        if not isinstance(data, dict):
            _LOGGER.error(
                "Chart data response is not a dictionary: %s",
                type(data).__name__,
            )
            raise RuntimeError("Invalid chart data response format")

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
                {
                    k: f"{type(v).__name__}[{len(v) if isinstance(v, list) else 'scalar'}]"
                    for k, v in data.items()
                    if k
                    in [
                        "meteringDatetime",
                        "actualConsumption",
                        "actualSupply",
                        "idleConsumption",
                        "idleSupply",
                    ]
                },
                str(e),
            )
            # Log detailed information about problematic data
            _LOGGER.error("Raw chart data field analysis:")
            for field in [
                "actualConsumption",
                "actualSupply",
                "idleConsumption",
                "idleSupply",
            ]:
                sample_info = _log_data_sample(data, field)
                _LOGGER.error("  %s: %s", field, sample_info)
            raise RuntimeError(f"Chart data validation failed: {str(e)}") from e

    def set_cached_pods(self, pods: list[PointOfDelivery]) -> None:
        """Seed the POD cache from an already-known, freshly discovered list.

        Lets a caller that already just called get_points_of_delivery()
        (e.g. the coordinator on startup) avoid a redundant API round-trip
        the next time this client needs to resolve a stable POD id — useful
        during a long-running statistics backfill, which can otherwise
        outlast PODS_CACHE_TTL and trigger repeated re-discovery.
        """
        self._pods_cache = pods
        self._pods_cache_ts = datetime.now(UTC)

    def _is_pods_cache_valid(self) -> bool:
        """Return True when the cached POD list is still valid."""
        if not self._pods_cache or not self._pods_cache_ts:
            return False
        return datetime.now(UTC) - self._pods_cache_ts <= PODS_CACHE_TTL

    async def _get_cached_pods(self) -> list[PointOfDelivery]:
        """Return cached PODs when fresh, otherwise fetch from API."""
        if self._is_pods_cache_valid():
            return self._pods_cache
        return await self.get_points_of_delivery()

    async def _get_pod_by_stable_id(self, pod_id: str) -> PointOfDelivery | None:
        """Return POD details by stable ID."""
        pods = await self._get_cached_pods()
        return next((pod for pod in pods if pod.id == pod_id), None)
