"""Config flow for Changan Deepal Cloud."""

from __future__ import annotations

import secrets
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import DeepalApiError, DeepalClient, DeepalRateLimitError
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ACTIVE_REFRESH_INTERVAL,
    CONF_APP_VERSION,
    CONF_CAC_TOKEN,
    CONF_CAC_USER_ID,
    CONF_CA_USER_ID,
    CONF_CONTROL_PIN,
    CONF_COUNTRY,
    CONF_DEVICE_ID,
    CONF_ENABLE_COMMANDS,
    CONF_LANGUAGE,
    CONF_PRIVATE_KEY,
    CONF_RC_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_USER_ID,
    CONF_VEHICLE_ID,
    DEFAULT_APP_VERSION,
    DEFAULT_ACTIVE_REFRESH_INTERVAL,
    DEFAULT_COUNTRY,
    DEFAULT_LANGUAGE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

COUNTRY_OPTIONS = {
    "GB": "United Kingdom (+44)",
    "PT": "Portugal (+351)",
}

COUNTRY_DIAL_CODES = {
    "GB": "44",
    "PT": "351",
}


class DeepalConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Deepal."""

    VERSION = 1
    _phone_login: dict[str, Any]
    _login_result: dict[str, Any]
    _reauth_entry: config_entries.ConfigEntry | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Start setup with the app-compatible phone/SMS flow."""
        return await self.async_step_phone(user_input)

    async def async_step_reauth(self, entry_data: dict[str, Any]):
        """Repair an existing entry whose app session has been invalidated."""
        entry_id = self.context.get("entry_id")
        self._reauth_entry = self.hass.config_entries.async_get_entry(entry_id) if entry_id else None
        return await self.async_step_phone()

    async def async_step_phone(self, user_input: dict[str, Any] | None = None):
        """Start the app-compatible phone/SMS login flow."""
        errors: dict[str, str] = {}
        if user_input is not None:
            selected_country = user_input.get(CONF_COUNTRY, DEFAULT_COUNTRY)
            country_code = COUNTRY_DIAL_CODES[selected_country]
            device_id = secrets.token_hex(16)
            private_key_pem, pub_key = DeepalClient.generate_login_keypair()
            client = DeepalClient(
                async_get_clientsession(self.hass),
                access_token="",
                refresh_token=None,
                country=selected_country,
                language=DEFAULT_LANGUAGE,
                app_version=DEFAULT_APP_VERSION,
                device_id=device_id,
                private_key_pem=private_key_pem,
            )
            try:
                await client.send_auth_code(
                    country_code=country_code,
                    mobile=user_input["mobile"],
                )
            except DeepalRateLimitError:
                errors["base"] = "too_many_codes"
            except DeepalApiError:
                errors["base"] = "send_code_failed"
            else:
                self._phone_login = {
                    **user_input,
                    "country_code": country_code,
                    CONF_COUNTRY: selected_country,
                    CONF_LANGUAGE: DEFAULT_LANGUAGE,
                    CONF_APP_VERSION: DEFAULT_APP_VERSION,
                    CONF_DEVICE_ID: device_id,
                    CONF_PRIVATE_KEY: private_key_pem,
                    "pub_key": pub_key,
                }
                return await self.async_step_sms()

        schema = vol.Schema(
            {
                vol.Required(CONF_COUNTRY, default=DEFAULT_COUNTRY): vol.In(COUNTRY_OPTIONS),
                vol.Required("mobile"): str,
            }
        )
        return self.async_show_form(
            step_id="phone",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_sms(self, user_input: dict[str, Any] | None = None):
        """Complete phone login with the SMS code."""
        errors: dict[str, str] = {}
        if user_input is not None:
            info = self._phone_login
            client = DeepalClient(
                async_get_clientsession(self.hass),
                access_token="",
                refresh_token=None,
                country=info.get(CONF_COUNTRY, DEFAULT_COUNTRY),
                language=info.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
                app_version=info.get(CONF_APP_VERSION, DEFAULT_APP_VERSION),
                device_id=info[CONF_DEVICE_ID],
                private_key_pem=info[CONF_PRIVATE_KEY],
            )
            try:
                login = await client.login_by_mobile_code(
                    country_code=info["country_code"],
                    mobile=info["mobile"],
                    auth_code=user_input["auth_code"],
                    sales_country=info.get(CONF_COUNTRY, DEFAULT_COUNTRY),
                    pub_key=info["pub_key"],
                )
                vehicles = await client.vehicles()
            except DeepalRateLimitError:
                errors["base"] = "too_many_codes"
            except DeepalApiError as err:
                if "APP_1_1_07_002" in str(err):
                    errors["base"] = "bad_sms_code"
                else:
                    errors["base"] = "login_failed"
            else:
                if not vehicles:
                    errors["base"] = "no_vehicles"
                else:
                    if self._reauth_entry is not None:
                        return await self._async_finish_reauth(login, vehicles, info)
                    self._login_result = {"login": login, "vehicles": vehicles, "info": info}
                    return await self.async_step_commands()

        schema = vol.Schema({vol.Required("auth_code"): str})
        return self.async_show_form(step_id="sms", data_schema=schema, errors=errors)

    async def async_step_commands(self, user_input: dict[str, Any] | None = None):
        """Choose whether remote commands should be enabled during setup."""
        errors: dict[str, str] = {}
        if user_input is not None:
            enable_commands = bool(user_input.get(CONF_ENABLE_COMMANDS, False))
            control_pin = str(user_input.get(CONF_CONTROL_PIN) or "").strip()
            if enable_commands and not control_pin:
                errors[CONF_CONTROL_PIN] = "pin_required"
            else:
                login = self._login_result["login"]
                vehicles = self._login_result["vehicles"]
                info = self._login_result["info"]
                vehicle_id = str(vehicles[0]["carId"])
                await self.async_set_unique_id(vehicle_id)
                self._abort_if_unique_id_configured()
                data = {
                    CONF_VEHICLE_ID: vehicle_id,
                    CONF_ACCESS_TOKEN: login["token"],
                    CONF_REFRESH_TOKEN: login.get("refreshToken"),
                    CONF_CAC_TOKEN: login.get("cacToken"),
                    CONF_USER_ID: login.get("userId"),
                    CONF_CA_USER_ID: login.get("caUserId"),
                    CONF_CAC_USER_ID: login.get("cacUserId"),
                    CONF_COUNTRY: info.get(CONF_COUNTRY, DEFAULT_COUNTRY),
                    CONF_LANGUAGE: info.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
                    CONF_APP_VERSION: info.get(CONF_APP_VERSION, DEFAULT_APP_VERSION),
                    CONF_DEVICE_ID: info[CONF_DEVICE_ID],
                    CONF_PRIVATE_KEY: info[CONF_PRIVATE_KEY],
                    CONF_ENABLE_COMMANDS: enable_commands,
                }
                if control_pin:
                    data[CONF_CONTROL_PIN] = control_pin
                title = vehicles[0].get("vin") or vehicles[0].get("modelName") or f"Deepal {vehicle_id}"
                return self.async_create_entry(title=title, data=data)

        schema = vol.Schema(
            {
                vol.Optional(CONF_ENABLE_COMMANDS, default=False): bool,
                vol.Optional(CONF_CONTROL_PIN): str,
            }
        )
        return self.async_show_form(step_id="commands", data_schema=schema, errors=errors)

    async def _async_finish_reauth(
        self,
        login: dict[str, Any],
        vehicles: list[dict[str, Any]],
        info: dict[str, Any],
    ):
        """Update the existing entry with fresh app-login material."""
        assert self._reauth_entry is not None
        existing_vehicle_id = str(self._reauth_entry.data[CONF_VEHICLE_ID])
        vehicle = next((item for item in vehicles if str(item.get("carId")) == existing_vehicle_id), None)
        if vehicle is None:
            return self.async_show_form(
                step_id="sms",
                data_schema=vol.Schema({vol.Required("auth_code"): str}),
                errors={"base": "wrong_account"},
            )

        new_data = dict(self._reauth_entry.data)
        new_data.update(
            {
                CONF_ACCESS_TOKEN: login["token"],
                CONF_REFRESH_TOKEN: login.get("refreshToken"),
                CONF_CAC_TOKEN: login.get("cacToken"),
                CONF_USER_ID: login.get("userId"),
                CONF_CA_USER_ID: login.get("caUserId"),
                CONF_CAC_USER_ID: login.get("cacUserId"),
                CONF_COUNTRY: info.get(CONF_COUNTRY, DEFAULT_COUNTRY),
                CONF_LANGUAGE: info.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
                CONF_APP_VERSION: info.get(CONF_APP_VERSION, DEFAULT_APP_VERSION),
                CONF_DEVICE_ID: info[CONF_DEVICE_ID],
                CONF_PRIVATE_KEY: info[CONF_PRIVATE_KEY],
            }
        )
        # rcToken is session-derived. A stored control PIN can mint a fresh rcToken later.
        new_data.pop(CONF_RC_TOKEN, None)
        return self.async_update_reload_and_abort(
            self._reauth_entry,
            data_updates=new_data,
            reason="reauth_successful",
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        """Create the options flow."""
        return DeepalOptionsFlow(config_entry)


class DeepalOptionsFlow(config_entries.OptionsFlow):
    """Options flow for Deepal."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get(CONF_ENABLE_COMMANDS) and not str(user_input.get(CONF_CONTROL_PIN) or "").strip():
                errors[CONF_CONTROL_PIN] = "pin_required"
            else:
                return self.async_create_entry(title="", data=user_input)

        data = self.config_entry.data | self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(CONF_SCAN_INTERVAL, default=data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=30,
                        max=3600,
                        mode=selector.NumberSelectorMode.BOX,
                        step=1,
                        unit_of_measurement="s",
                    )
                ),
                vol.Optional(CONF_ACTIVE_REFRESH_INTERVAL, default=data.get(CONF_ACTIVE_REFRESH_INTERVAL, DEFAULT_ACTIVE_REFRESH_INTERVAL)): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=60,
                        max=3600,
                        mode=selector.NumberSelectorMode.BOX,
                        step=1,
                        unit_of_measurement="s",
                    )
                ),
                vol.Optional(CONF_CONTROL_PIN, default=data.get(CONF_CONTROL_PIN, "")): str,
                vol.Optional(CONF_ENABLE_COMMANDS, default=data.get(CONF_ENABLE_COMMANDS, False)): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
