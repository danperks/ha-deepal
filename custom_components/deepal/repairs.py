"""Repairs for the Changan Deepal Cloud integration."""

from __future__ import annotations

import voluptuous as vol

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .api import DeepalApiError
from .const import CONF_QR_AUTH_URL

_COMMAND_REAUTH_SUFFIX = "_command_reauth"
_LOCATION_PERMISSION_SUFFIX = "_location_permission"


class DeepalCommandReauthRepairFlow(RepairsFlow):
    """Confirm, then start reauthentication to register a fresh command key."""

    async def async_step_init(self, user_input: dict[str, str] | None = None) -> data_entry_flow.FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, str] | None = None) -> data_entry_flow.FlowResult:
        if user_input is not None:
            entry = self.hass.config_entries.async_get_entry(self.issue_id.removesuffix(_COMMAND_REAUTH_SUFFIX))
            if entry is not None:
                entry.async_start_reauth(self.hass)
            return self.async_create_entry(data={})
        return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}))


class DeepalLocationPermissionRepairFlow(RepairsFlow):
    """Paste the head-unit QR URL to turn location sharing on."""

    async def async_step_init(self, user_input: dict[str, str] | None = None) -> data_entry_flow.FlowResult:
        return await self.async_step_authorize(user_input)

    async def async_step_authorize(self, user_input: dict[str, str] | None = None) -> data_entry_flow.FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            entry_id = self.issue_id.removesuffix(_LOCATION_PERMISSION_SUFFIX)
            entry = self.hass.config_entries.async_get_entry(entry_id)
            coordinator = getattr(entry, "runtime_data", None) if entry is not None else None
            try:
                if coordinator is None:
                    raise DeepalApiError("Deepal entry is not loaded")
                await coordinator.async_authorize_qr_code(user_input[CONF_QR_AUTH_URL])
            except (DeepalApiError, HomeAssistantError):
                errors["base"] = "qr_auth_failed"
            else:
                return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="authorize",
            data_schema=vol.Schema({vol.Required(CONF_QR_AUTH_URL): str}),
            errors=errors or None,
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create the fix flow for a Deepal repair issue."""
    if issue_id.endswith(_LOCATION_PERMISSION_SUFFIX):
        return DeepalLocationPermissionRepairFlow()
    return DeepalCommandReauthRepairFlow()
