"""Changan Deepal Cloud integration."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import DeepalApiError, DeepalClient
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_CAC_TOKEN,
    CONF_CAC_USER_ID,
    CONF_CA_USER_ID,
    CONF_COUNTRY,
    CONF_CONTROL_PIN,
    CONF_DEVICE_ID,
    CONF_ENABLE_API_LOGGING,
    CONF_ENABLE_COMMANDS,
    CONF_LANGUAGE,
    CONF_PRIVATE_KEY,
    CONF_RC_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_USER_ID,
    CONF_VEHICLE_ID,
    DEFAULT_APP_VERSION,
    DEFAULT_COUNTRY,
    DEFAULT_LANGUAGE,
    DOMAIN,
    PLATFORMS,
    SERVICE_AUTHORIZE_QR,
)
from .coordinator import DeepalDataUpdateCoordinator, should_start_live_mqtt

_AUTHORIZE_QR_SCHEMA = vol.Schema(
    {
        vol.Required("qr_code"): str,
        vol.Optional("entry_id"): str,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Deepal from a config entry."""
    session = async_get_clientsession(hass)
    data = entry.data | entry.options
    client = DeepalClient(
        session,
        access_token=data[CONF_ACCESS_TOKEN],
        refresh_token=data.get(CONF_REFRESH_TOKEN),
        country=data.get(CONF_COUNTRY, DEFAULT_COUNTRY),
        language=data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
        app_version=DEFAULT_APP_VERSION,
        device_id=data[CONF_DEVICE_ID],
        private_key_pem=data.get(CONF_PRIVATE_KEY),
        enable_commands=data.get(CONF_ENABLE_COMMANDS, False),
        enable_api_logging=data.get(CONF_ENABLE_API_LOGGING, False),
        rc_token=data.get(CONF_RC_TOKEN),
        control_pin=data.get(CONF_CONTROL_PIN),
        cac_token=data.get(CONF_CAC_TOKEN),
        user_id=data.get(CONF_USER_ID) or data.get(CONF_CA_USER_ID) or data.get(CONF_CAC_USER_ID),
    )
    coordinator = DeepalDataUpdateCoordinator(hass, entry, client, str(data[CONF_VEHICLE_ID]))
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    vehicle = (coordinator.data or {}).get("vehicle") or {}
    if should_start_live_mqtt(entry.options, vehicle):
        coordinator.start_live_mqtt()
        entry.async_on_unload(coordinator.async_stop_live_mqtt)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    _async_register_authorize_qr_service(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        _async_remove_authorize_qr_service_if_unused(hass, entry)
    return unload_ok


def _async_register_authorize_qr_service(hass: HomeAssistant) -> None:
    """Register deepal.authorize_qr once, shared by every loaded entry."""
    if hass.services.has_service(DOMAIN, SERVICE_AUTHORIZE_QR):
        return

    async def _async_handle_authorize_qr(call: ServiceCall) -> None:
        await async_handle_authorize_qr(hass, call)

    hass.services.async_register(
        DOMAIN,
        SERVICE_AUTHORIZE_QR,
        _async_handle_authorize_qr,
        schema=_AUTHORIZE_QR_SCHEMA,
    )


async def async_handle_authorize_qr(hass: HomeAssistant, call: ServiceCall) -> None:
    """Authorise a head-unit location QR code for one loaded Deepal entry."""
    entry_id = call.data.get("entry_id")
    if entry_id:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
            raise HomeAssistantError("Deepal config entry was not found or is not loaded")
    else:
        entries = hass.config_entries.async_loaded_entries(DOMAIN)
        if len(entries) != 1:
            if entries:
                raise HomeAssistantError(
                    "Multiple Deepal entries are loaded; pass entry_id to deepal.authorize_qr"
                )
            raise HomeAssistantError("No loaded Deepal entry is available for deepal.authorize_qr")
        entry = entries[0]
    coordinator = getattr(entry, "runtime_data", None)
    if coordinator is None:
        raise HomeAssistantError("Deepal entry is not loaded")
    try:
        await coordinator.async_authorize_qr_code(call.data["qr_code"])
    except DeepalApiError as err:
        raise HomeAssistantError(f"Deepal QR authorisation failed: {err}") from err


def _async_remove_authorize_qr_service_if_unused(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop the shared QR service when the last Deepal entry unloads."""
    if any(
        item.entry_id != entry.entry_id for item in hass.config_entries.async_loaded_entries(DOMAIN)
    ):
        return
    if hass.services.has_service(DOMAIN, SERVICE_AUTHORIZE_QR):
        hass.services.async_remove(DOMAIN, SERVICE_AUTHORIZE_QR)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the integration when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
