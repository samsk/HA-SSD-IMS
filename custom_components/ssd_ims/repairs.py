"""Repair flows for SSD IMS fixable issues."""

from __future__ import annotations

from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.core import HomeAssistant

from .coordinator import SsdImsDataCoordinator


class ImportPausedRepairFlow(RepairsFlow):
    """Confirm reimport of the day that paused statistics import."""

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Start the fix at the confirm step."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Ask for confirm, then reimport from the paused day."""
        if user_input is not None:
            entry_id = (self.data or {}).get("entry_id")
            entry = (
                self.hass.config_entries.async_get_entry(entry_id) if entry_id else None
            )
            coordinator = getattr(entry, "runtime_data", None) if entry else None
            if isinstance(coordinator, SsdImsDataCoordinator):
                await coordinator.async_reimport()
            return self.async_create_entry(data={})

        placeholders = {
            "pod_name": "?",
            "day": "?",
            "reason": "?",
        }
        entry_id = (self.data or {}).get("entry_id")
        entry = self.hass.config_entries.async_get_entry(entry_id) if entry_id else None
        coordinator = getattr(entry, "runtime_data", None) if entry else None
        if isinstance(coordinator, SsdImsDataCoordinator) and coordinator._import_pause:
            placeholders.update(
                {
                    "pod_name": str(coordinator._import_pause.get("pod_name", "?")),
                    "day": str(coordinator._import_pause.get("day", "?")),
                    "reason": str(coordinator._import_pause.get("reason", "?")),
                }
            )

        return self.async_show_form(
            step_id="confirm",
            description_placeholders=placeholders,
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create the repair flow for a fixable issue."""
    return ImportPausedRepairFlow()
