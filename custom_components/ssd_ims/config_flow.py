"""Configuration flow for SSD IMS integration."""
import logging
import re
from typing import Any, Dict, List, Optional

import voluptuous as vol
from aiohttp import ClientSession
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .api_client import SsdImsApiClient
from .const import (CONF_ENABLE_IDLE_SENSORS, CONF_ENABLE_SUPPLY_SENSORS,
                    CONF_POD_NAME_MAPPING, CONF_POINT_OF_DELIVERY,
                    CONF_SCAN_INTERVAL, DEFAULT_ENABLE_IDLE_SENSORS,
                    DEFAULT_ENABLE_SUPPLY_SENSORS, DEFAULT_SCAN_INTERVAL,
                    DOMAIN, NAME, POD_NAME_MAX_LENGTH, POD_NAME_PATTERN,
                    SCAN_INTERVAL_OPTIONS)
from .models import PointOfDelivery

_LOGGER = logging.getLogger(__name__)


class SsdImsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle SSD IMS configuration flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize config flow."""
        self._username: Optional[str] = None
        self._password: Optional[str] = None
        self._scan_interval: Optional[int] = None
        self._pods: Optional[List[PointOfDelivery]] = None
        self._selected_pods: Optional[List[str]] = None
        self._pod_name_mapping: Optional[Dict[str, str]] = None
        self._enable_supply_sensors: Optional[bool] = None
        self._enable_idle_sensors: Optional[bool] = None

    async def async_step_user(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Handle initial user configuration step."""
        errors = {}

        if user_input is not None:
            self._username = user_input[CONF_USERNAME]
            self._password = user_input[CONF_PASSWORD]
            self._scan_interval = user_input.get(
                CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
            )

            try:
                # Test authentication
                async with ClientSession() as session:
                    api_client = SsdImsApiClient(session)
                    if await api_client.authenticate(self._username, self._password):
                        # Get available PODs
                        self._pods = await api_client.get_points_of_delivery()
                        return await self.async_step_point_of_delivery()
                    else:
                        errors["base"] = "invalid_auth"
            except Exception as e:
                _LOGGER.error("Error during authentication: %s", e)
                errors["base"] = "cannot_connect"

        # Show form
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Optional(
                        CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
                    ): vol.In(SCAN_INTERVAL_OPTIONS),
                }
            ),
            errors=errors,
        )

    async def async_step_point_of_delivery(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Handle point of delivery selection step."""
        errors = {}

        if user_input is not None:
            selected_pods = user_input.get("selected_pods", [])
            if not selected_pods:
                errors["base"] = "no_pods_selected"
            else:
                self._selected_pods = selected_pods
                return await self.async_step_pod_naming()

        # Create POD selection options using stable pod.id instead of session pod.value
        pod_options = {}
        for pod in self._pods:
            try:
                pod_id = pod.id  # Use stable 16-20 char ID
                pod_options[pod_id] = pod.text
            except ValueError as e:
                _LOGGER.warning(
                    "Skipping POD with invalid ID format: %s - %s", pod.text, e
                )
                continue

        return self.async_show_form(
            step_id="point_of_delivery",
            data_schema=vol.Schema(
                {
                    vol.Required("selected_pods"): vol.All(
                        cv.multi_select(pod_options), vol.Length(min=1)
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_pod_naming(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Handle POD naming configuration step."""
        errors = {}

        if user_input is not None:
            pod_name_mapping = {}
            friendly_names = set()

            # Validate POD names using stable pod.id
            for pod_id in self._selected_pods:
                friendly_name = user_input.get(f"pod_name_{pod_id}", "").strip()

                if friendly_name:
                    # Validate name format
                    if not re.match(POD_NAME_PATTERN, friendly_name):
                        errors[f"pod_name_{pod_id}"] = "invalid_format"
                        continue

                    # Check length
                    if len(friendly_name) > POD_NAME_MAX_LENGTH:
                        errors[f"pod_name_{pod_id}"] = "too_long"
                        continue

                    # Check uniqueness
                    if friendly_name in friendly_names:
                        errors[f"pod_name_{pod_id}"] = "duplicate_name"
                        continue

                    friendly_names.add(friendly_name)
                    pod_name_mapping[pod_id] = friendly_name

            if not errors:
                self._pod_name_mapping = pod_name_mapping
                return await self.async_step_sensor_options()

        # Create POD naming form using stable pod.id
        schema_fields = {}
        for pod_id in self._selected_pods:
            # Find pod by stable ID
            pod = next((p for p in self._pods if p.id == pod_id), None)
            if pod:
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
            # Find pod by stable ID
            pod = next((p for p in self._pods if p.id == pod_id), None)
            if pod:
                info_lines.append(f"• {pod.text} → {pod_id}")
        return "\n".join(info_lines)

    async def async_step_sensor_options(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Handle sensor options configuration step."""
        errors = {}

        if user_input is not None:
            self._enable_supply_sensors = user_input.get(
                CONF_ENABLE_SUPPLY_SENSORS, DEFAULT_ENABLE_SUPPLY_SENSORS
            )
            self._enable_idle_sensors = user_input.get(
                CONF_ENABLE_IDLE_SENSORS, DEFAULT_ENABLE_IDLE_SENSORS
            )

            # Create config entry
            config_data = {
                CONF_USERNAME: self._username,
                CONF_PASSWORD: self._password,
                CONF_SCAN_INTERVAL: self._scan_interval,
                CONF_POINT_OF_DELIVERY: self._selected_pods,
                CONF_POD_NAME_MAPPING: self._pod_name_mapping,
                CONF_ENABLE_SUPPLY_SENSORS: self._enable_supply_sensors,
                CONF_ENABLE_IDLE_SENSORS: self._enable_idle_sensors,
            }

            return self.async_create_entry(
                title=f"{NAME} ({self._username})",
                data=config_data,
            )

        return self.async_show_form(
            step_id="sensor_options",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_ENABLE_SUPPLY_SENSORS,
                        default=DEFAULT_ENABLE_SUPPLY_SENSORS,
                    ): bool,
                    vol.Optional(
                        CONF_ENABLE_IDLE_SENSORS,
                        default=DEFAULT_ENABLE_IDLE_SENSORS,
                    ): bool,
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
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Manage the options."""
        errors = {}

        if user_input is not None:
            # Update config entry
            new_data = self.config_entry.data.copy()
            new_data[CONF_SCAN_INTERVAL] = user_input[CONF_SCAN_INTERVAL]
            new_data[CONF_ENABLE_SUPPLY_SENSORS] = user_input.get(
                CONF_ENABLE_SUPPLY_SENSORS,
                self.config_entry.data.get(
                    CONF_ENABLE_SUPPLY_SENSORS, DEFAULT_ENABLE_SUPPLY_SENSORS
                ),
            )
            new_data[CONF_ENABLE_IDLE_SENSORS] = user_input.get(
                CONF_ENABLE_IDLE_SENSORS,
                self.config_entry.data.get(
                    CONF_ENABLE_IDLE_SENSORS, DEFAULT_ENABLE_IDLE_SENSORS
                ),
            )

            # Update config entry
            self.hass.config_entries.async_update_entry(
                self.config_entry, data=new_data
            )

            # Update coordinator configuration and trigger refresh if needed
            from .coordinator import SsdImsDataCoordinator
            coordinator: SsdImsDataCoordinator = self.hass.data[DOMAIN][self.config_entry.entry_id]
            await coordinator.update_config(new_data)

            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SCAN_INTERVAL,
                        default=self.config_entry.data.get(
                            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                        ),
                    ): vol.In(SCAN_INTERVAL_OPTIONS),
                    vol.Optional(
                        CONF_ENABLE_SUPPLY_SENSORS,
                        default=self.config_entry.data.get(
                            CONF_ENABLE_SUPPLY_SENSORS, DEFAULT_ENABLE_SUPPLY_SENSORS
                        ),
                    ): bool,
                    vol.Optional(
                        CONF_ENABLE_IDLE_SENSORS,
                        default=self.config_entry.data.get(
                            CONF_ENABLE_IDLE_SENSORS, DEFAULT_ENABLE_IDLE_SENSORS
                        ),
                    ): bool,
                }
            ),
            errors=errors,
        )


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect to the host."""
