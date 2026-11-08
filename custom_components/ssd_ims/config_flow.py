"""Configuration flow for SSD IMS integration."""

import asyncio
import logging
from typing import Any

import voluptuous as vol
from aiohttp import ClientError
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api_client import SsdImsApiClient
from .const import (
    CONF_ENABLE_HISTORY_IMPORT,
    CONF_HISTORY_DAYS,
    CONF_POD_NAME_MAPPING,
    CONF_POINT_OF_DELIVERY,
    CONF_PRICE_NT,
    CONF_PRICE_VT,
    CONF_SCAN_INTERVAL,
    CURRENCY,
    DEFAULT_ENABLE_HISTORY_IMPORT,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    NAME,
    POD_NAME_MAX_LENGTH,
    SCAN_INTERVAL_OPTIONS,
)
from .helpers import sanitize_name
from .models import PointOfDelivery

_LOGGER = logging.getLogger(__name__)


def _scan_interval_selector() -> SelectSelector:
    """Update-interval choice with labels from the translation files.

    Select options are strings, so the chosen value is converted back to
    minutes (int) by _scan_interval_minutes before it's stored.
    """
    return SelectSelector(
        SelectSelectorConfig(
            options=[str(minutes) for minutes in SCAN_INTERVAL_OPTIONS],
            translation_key=CONF_SCAN_INTERVAL,
            mode=SelectSelectorMode.LIST,
        )
    )


def _scan_interval_minutes(value: Any) -> int:
    """Convert a selected update interval back to minutes."""
    minutes = int(value)
    if minutes not in SCAN_INTERVAL_OPTIONS:
        raise vol.Invalid(f"Unsupported update interval: {value}")
    return minutes


class SsdImsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle SSD IMS configuration flow."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialize config flow."""
        self._username: str | None = None
        self._password: str | None = None
        self._pods: list[PointOfDelivery] | None = None
        self._selected_pods: list[str] | None = None
        self._pod_name_mapping: dict[str, str] | None = None
        self._scan_interval: int | None = None
        self._enable_history_import: bool | None = None
        self._history_days: int | None = None
        self._reconfiguring: bool = False

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle initial user configuration step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._username = user_input[CONF_USERNAME]
            self._password = user_input[CONF_PASSWORD]

            try:
                api_client = SsdImsApiClient(async_get_clientsession(self.hass))
                if await api_client.authenticate(self._username, self._password):
                    await self.async_set_unique_id(self._username.lower())
                    self._abort_if_unique_id_configured()
                    self._pods = await api_client.get_points_of_delivery()
                    return await self.async_step_point_of_delivery()
                else:
                    errors["base"] = "invalid_auth"
            except AbortFlow:
                raise
            except (ClientError, asyncio.TimeoutError, RuntimeError) as e:
                _LOGGER.error("Error during authentication: %s", e)
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during authentication")
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Handle re-authentication when credentials are invalid."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Confirm re-authentication with new credentials."""
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()

        if user_input is not None:
            try:
                api_client = SsdImsApiClient(async_get_clientsession(self.hass))
                if await api_client.authenticate(
                    user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
                ):
                    return self.async_update_reload_and_abort(
                        reauth_entry,
                        data_updates={
                            CONF_USERNAME: user_input[CONF_USERNAME],
                            CONF_PASSWORD: user_input[CONF_PASSWORD],
                        },
                    )
                errors["base"] = "invalid_auth"
            except (ClientError, asyncio.TimeoutError, RuntimeError) as e:
                _LOGGER.error("Error during re-authentication: %s", e)
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during re-authentication")
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME,
                        default=reauth_entry.data.get(CONF_USERNAME, ""),
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "username": reauth_entry.data.get(CONF_USERNAME, ""),
            },
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle reconfiguration — update POD selection and names."""
        self._reconfiguring = True
        current_entry = self._get_reconfigure_entry()
        self._username = current_entry.data.get(CONF_USERNAME)
        self._password = current_entry.data.get(CONF_PASSWORD)

        try:
            api_client = SsdImsApiClient(async_get_clientsession(self.hass))
            if not await api_client.authenticate(self._username, self._password):
                return self.async_abort(reason="reauth_required")
            self._pods = await api_client.get_points_of_delivery()
        except (ClientError, asyncio.TimeoutError, RuntimeError) as e:
            _LOGGER.error("Error fetching PODs during reconfigure: %s", e)
            return self.async_abort(reason="cannot_connect")
        except Exception:
            _LOGGER.exception("Unexpected error during reconfigure")
            return self.async_abort(reason="cannot_connect")

        return await self.async_step_point_of_delivery()

    async def async_step_point_of_delivery(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle point of delivery selection step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            if not (selected_pods := user_input.get("selected_pods", [])):
                errors["base"] = "no_pods_selected"
            else:
                self._selected_pods = selected_pods
                return await self.async_step_pod_naming()

        # Create POD selection options using stable pod.id instead of session
        # pod.value. get_points_of_delivery() already filters out any POD it
        # couldn't parse a stable ID for, so every pod here is valid.
        pod_options = {pod.id: pod.text for pod in self._pods}

        # When reconfiguring, pre-select the currently configured PODs via schema default
        if self._reconfiguring:
            current_entry = self._get_reconfigure_entry()
            current_pods = current_entry.data.get(CONF_POINT_OF_DELIVERY, [])
            pods_field = vol.Optional("selected_pods", default=current_pods)
        else:
            pods_field = vol.Required("selected_pods")

        return self.async_show_form(
            step_id="point_of_delivery",
            data_schema=vol.Schema(
                {
                    # No vol.Length(min=1) here: an empty selection needs to
                    # reach the handler above so it can show the friendlier
                    # "no_pods_selected" error, rather than voluptuous
                    # rejecting it first with a generic schema-validation
                    # error the user never sees a translated message for.
                    pods_field: cv.multi_select(pod_options),
                }
            ),
            errors=errors,
        )

    async def async_step_pod_naming(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle POD naming configuration step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            pod_name_mapping: dict[str, str] = {}
            sanitized_names: set[str] = set()

            for pod_id in self._selected_pods:
                if display_name := user_input.get(f"pod_name_{pod_id}", "").strip():
                    if len(display_name) > POD_NAME_MAX_LENGTH:
                        errors[f"pod_name_{pod_id}"] = "too_long"
                        continue

                    sanitized_lower = sanitize_name(display_name)

                    if not sanitized_lower:
                        errors[f"pod_name_{pod_id}"] = "invalid_format"
                        continue

                    if sanitized_lower in sanitized_names:
                        errors[f"pod_name_{pod_id}"] = "duplicate_name"
                        continue

                    sanitized_names.add(sanitized_lower)
                    pod_name_mapping[pod_id] = display_name

            if not errors:
                self._pod_name_mapping = pod_name_mapping
                return await self.async_step_history_import()

        current_names: dict[str, str] = {}
        if self._reconfiguring:
            current_entry = self._get_reconfigure_entry()
            current_names = current_entry.data.get(CONF_POD_NAME_MAPPING, {})

        schema_fields: dict = {}
        for pod_id in self._selected_pods:
            if next((p for p in self._pods if p.id == pod_id), None) is not None:
                if pod_id in current_names:
                    schema_fields[
                        vol.Optional(
                            f"pod_name_{pod_id}", default=current_names[pod_id]
                        )
                    ] = str
                    continue

                schema_fields[vol.Optional(f"pod_name_{pod_id}")] = str

        return self.async_show_form(
            step_id="pod_naming",
            data_schema=vol.Schema(schema_fields),
            errors=errors,
            description_placeholders={"pod_info": self._get_pod_info_text()},
        )

    def _get_pod_info_text(self) -> str:
        """Generate POD information text for the form."""
        info_lines = []
        for pod_id in self._selected_pods:
            if pod := next((p for p in self._pods if p.id == pod_id), None):
                info_lines.append(f"• {pod.text} → {pod_id}")
        return "\n".join(info_lines)

    async def async_step_history_import(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle final configuration step with update interval and history import."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._scan_interval = _scan_interval_minutes(
                user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            )
            self._enable_history_import = user_input.get(
                CONF_ENABLE_HISTORY_IMPORT, DEFAULT_ENABLE_HISTORY_IMPORT
            )
            self._history_days = user_input.get(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS)

            config_data = {
                CONF_USERNAME: self._username,
                CONF_PASSWORD: self._password,
                CONF_SCAN_INTERVAL: self._scan_interval,
                CONF_POINT_OF_DELIVERY: self._selected_pods,
                CONF_POD_NAME_MAPPING: self._pod_name_mapping,
                CONF_HISTORY_DAYS: self._history_days
                if self._enable_history_import
                else 0,
            }

            if self._reconfiguring:
                return self.async_update_reload_and_abort(
                    self._get_reconfigure_entry(),
                    data_updates=config_data,
                    reason="reconfigure_successful",
                )

            return self.async_create_entry(
                title=f"{NAME} ({self._username})",
                data=config_data,
            )

        # Pre-populate with current settings when reconfiguring
        default_scan_interval = DEFAULT_SCAN_INTERVAL
        default_history_days = DEFAULT_HISTORY_DAYS
        default_enable_history_import = DEFAULT_ENABLE_HISTORY_IMPORT
        if self._reconfiguring:
            current_entry = self._get_reconfigure_entry()
            default_scan_interval = current_entry.options.get(
                CONF_SCAN_INTERVAL,
                current_entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            )
            stored_days = current_entry.data.get(
                CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS
            )
            default_enable_history_import = stored_days > 0
            default_history_days = (
                stored_days if stored_days > 0 else DEFAULT_HISTORY_DAYS
            )

        return self.async_show_form(
            step_id="history_import",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SCAN_INTERVAL, default=str(default_scan_interval)
                    ): _scan_interval_selector(),
                    vol.Optional(
                        CONF_ENABLE_HISTORY_IMPORT,
                        default=default_enable_history_import,
                    ): bool,
                    vol.Optional(
                        CONF_HISTORY_DAYS,
                        default=default_history_days,
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=365)),
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Get options flow for this handler."""
        return SsdImsOptionsFlow()


class SsdImsOptionsFlow(config_entries.OptionsFlow):
    """Handle SSD IMS options flow."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input = {
                **user_input,
                CONF_SCAN_INTERVAL: _scan_interval_minutes(
                    user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
                ),
            }
            # Update coordinator configuration
            coordinator = self.config_entry.runtime_data
            await coordinator.update_config({**self.config_entry.data, **user_input})
            # Apply new prices/settings now rather than at the next scheduled
            # poll, which can be up to a day away.
            await coordinator.async_request_refresh()
            return self.async_create_entry(title="", data=user_input)

        price_selector = NumberSelector(
            NumberSelectorConfig(
                min=0,
                max=100,
                step="any",
                mode=NumberSelectorMode.BOX,
                unit_of_measurement=f"{CURRENCY}/kWh",
            )
        )
        options = self.config_entry.options

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SCAN_INTERVAL,
                        default=str(
                            options.get(
                                CONF_SCAN_INTERVAL,
                                self.config_entry.data.get(
                                    CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                                ),
                            )
                        ),
                    ): _scan_interval_selector(),
                    # suggested_value (not default) so the field can be
                    # cleared again to stop importing costs.
                    vol.Optional(
                        CONF_PRICE_VT,
                        description={"suggested_value": options.get(CONF_PRICE_VT)},
                    ): price_selector,
                    vol.Optional(
                        CONF_PRICE_NT,
                        description={"suggested_value": options.get(CONF_PRICE_NT)},
                    ): price_selector,
                }
            ),
            errors=errors,
        )


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect to the host."""
