"""Data coordinator for Deepal vehicles."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import DeepalApiError, DeepalAuthError, DeepalClient, _as_int
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ACTIVE_REFRESH_INTERVAL,
    CONF_ENABLE_LIVE_MQTT,
    CONF_REFRESH_TOKEN,
    DEFAULT_ACTIVE_REFRESH_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .live_mqtt import S05LiveMqttSession

_LOGGER = logging.getLogger(__name__)


def should_start_live_mqtt(options: Mapping[str, Any], vehicle: dict[str, Any]) -> bool:
    """Return whether this entry should run the supervised S05 MQTT session.

    The option defaults to off. Only a literal ``protocolType`` of ``MQTT``
    (any case) qualifies. ``TLV`` and ``SDA-MQTT`` do not.
    """
    if not options.get(CONF_ENABLE_LIVE_MQTT, False):
        return False
    return str(vehicle.get("protocolType") or "").upper() == "MQTT"


class DeepalDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetch vehicle list and condition data."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: DeepalClient, vehicle_id: str) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)),
        )
        self.entry = entry
        self.client = client
        self.vehicle_id = vehicle_id
        self.refresh_failure_count = 0
        self.last_refresh_failure: str | None = None
        self._command_in_progress = False
        self._next_active_refresh_at = 0.0
        self._live_mqtt_task: asyncio.Task[None] | None = None
        self._live_session: S05LiveMqttSession | None = None

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            vehicles, condition = await self._async_fetch()
        except DeepalAuthError as err:
            try:
                tokens = await self.client.refresh_tokens()
                new_data = dict(self.entry.data)
                new_data[CONF_ACCESS_TOKEN] = tokens.access_token
                if tokens.refresh_token:
                    new_data[CONF_REFRESH_TOKEN] = tokens.refresh_token
                self.hass.config_entries.async_update_entry(self.entry, data=new_data)
                vehicles, condition = await self._async_fetch()
            except (DeepalApiError, DeepalAuthError) as refresh_err:
                self._record_refresh_failure(refresh_err)
                raise ConfigEntryAuthFailed(f"Authentication failed: {refresh_err}") from refresh_err
        except DeepalApiError as err:
            self._record_refresh_failure(err)
            raise UpdateFailed(str(err)) from err

        self.last_refresh_failure = None
        vehicle = self._vehicle_from_list(vehicles)
        self._async_sync_location_permission_issue(condition)
        return {"vehicles": vehicles, "vehicle": vehicle or {}, "condition": condition}

    async def async_authorize_qr_code(self, qr_code_or_url: str) -> dict[str, Any]:
        """Authorise a head-unit location QR code, then refresh vehicle data.

        Token refresh matches ``_async_update_data``: one refresh and config-entry
        update on ``DeepalAuthError``, then a single retry.
        """
        try:
            result = await self.client.authorize_qr_code(qr_code_or_url)
        except DeepalAuthError:
            try:
                tokens = await self.client.refresh_tokens()
                new_data = dict(self.entry.data)
                new_data[CONF_ACCESS_TOKEN] = tokens.access_token
                if tokens.refresh_token:
                    new_data[CONF_REFRESH_TOKEN] = tokens.refresh_token
                self.hass.config_entries.async_update_entry(self.entry, data=new_data)
                result = await self.client.authorize_qr_code(qr_code_or_url)
            except (DeepalApiError, DeepalAuthError) as refresh_err:
                raise ConfigEntryAuthFailed(f"Authentication failed: {refresh_err}") from refresh_err
        ir.async_delete_issue(self.hass, DOMAIN, f"{self.entry.entry_id}_location_permission")
        await self.async_request_refresh()
        return result

    def _async_sync_location_permission_issue(self, condition: dict[str, Any]) -> None:
        """Warn while the head unit is not sharing location, and clear it when it is."""
        loc = condition.get("locationGeo") if isinstance(condition, dict) else None
        if not isinstance(loc, dict):
            return
        issue_id = f"{self.entry.entry_id}_location_permission"
        switch = _as_int(loc.get("locationSwitch"))
        if switch == 0:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=True,
                is_persistent=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key="location_permission",
            )
        elif switch == 1:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    def start_live_mqtt(self) -> None:
        """Start the supervised MQTT listener as a config-entry background task."""
        if self._live_mqtt_task is not None and not self._live_mqtt_task.done():
            return
        self._live_mqtt_task = self.entry.async_create_background_task(
            self.hass,
            self._async_live_mqtt_loop(),
            name=f"deepal_live_mqtt_{self.entry.entry_id}",
        )

    async def async_stop_live_mqtt(self) -> None:
        """Cancel the live listener and wait until its socket has closed."""
        session = self._live_session
        if session is not None:
            session.request_stop()
        task = self._live_mqtt_task
        self._live_mqtt_task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return

    async def _async_live_mqtt_loop(self) -> None:
        session = S05LiveMqttSession(
            self.client,
            self.vehicle_id,
            on_condition=self._on_live_condition,
            refresh_auth=self._async_refresh_live_mqtt_auth,
        )
        self._live_session = session
        try:
            await session.run()
        finally:
            if self._live_session is session:
                self._live_session = None

    def _on_live_condition(self, condition: dict[str, Any]) -> None:
        """Apply a coalesced push without logging the payload."""
        if not self.data:
            return
        self._async_set_condition(condition)
        self._async_sync_location_permission_issue(condition)

    async def _async_refresh_live_mqtt_auth(self) -> bool:
        """Refresh tokens after ``getConnConf`` or the MQTT token call rejects them."""
        try:
            tokens = await self.client.refresh_tokens()
        except (DeepalApiError, DeepalAuthError) as err:
            _LOGGER.warning("Deepal live MQTT token refresh failed (%s)", type(err).__name__)
            return False
        new_data = dict(self.entry.data)
        new_data[CONF_ACCESS_TOKEN] = tokens.access_token
        if tokens.refresh_token:
            new_data[CONF_REFRESH_TOKEN] = tokens.refresh_token
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)
        return True

    async def _async_fetch(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Fetch vehicle metadata and condition.

        While the live MQTT session is healthy, this only needs the vehicle
        list. Condition updates arrive on the open socket. A disconnected
        session falls back to the one-shot exchange, which waits for the
        shared client id instead of connecting beside the listener.
        """
        vehicles = await self.client.vehicles()
        vehicle = self._vehicle_from_list(vehicles) or {}
        if self._vehicle_uses_mqtt(vehicle):
            condition = await self._async_mqtt_condition()
        else:
            await self._async_maybe_active_condition_refresh()
            condition = await self.client.condition(self.vehicle_id)
        return vehicles, condition

    async def _async_mqtt_condition(self) -> dict[str, Any]:
        """Use the live snapshot when the session is up, otherwise one-shot MQTT."""
        live = self.client.live_mqtt
        if live is not None and live.healthy:
            latest = live.latest_condition()
            if isinstance(latest, dict) and latest:
                return latest
        return await self.client.s05_mqtt_condition(self.vehicle_id)

    def _vehicle_from_list(self, vehicles: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Return the configured vehicle metadata from a vehicle list response."""
        return next((item for item in vehicles if str(item.get("carId")) == self.vehicle_id), None)

    @staticmethod
    def _vehicle_uses_mqtt(vehicle: dict[str, Any]) -> bool:
        """Return whether the app backend declares this vehicle as MQTT-backed."""
        return str(vehicle.get("protocolType") or "").upper() == "MQTT"

    @property
    def vehicle_uses_mqtt(self) -> bool:
        """Return whether the configured vehicle uses MQTT telemetry."""
        vehicle = (self.data or {}).get("vehicle") or {}
        return self._vehicle_uses_mqtt(vehicle)

    async def _async_maybe_active_condition_refresh(self) -> None:
        """Periodically ask the car/cloud for fresh condition data."""
        if not self.client.commands_enabled:
            return

        loop = asyncio.get_running_loop()
        now = loop.time()
        if now < self._next_active_refresh_at:
            return

        interval = self.entry.options.get(CONF_ACTIVE_REFRESH_INTERVAL, DEFAULT_ACTIVE_REFRESH_INTERVAL)
        self._next_active_refresh_at = now + interval
        try:
            await self.async_execute_command(
                lambda: self.client.control_condition_inquiry(vehicle_id=self.vehicle_id),
                raise_if_busy=False,
                timeout=30,
            )
        except (DeepalApiError, DeepalAuthError, HomeAssistantError) as err:
            _LOGGER.warning("Deepal active condition refresh failed: %s", err)

    async def async_execute_command(
        self,
        send_command: Callable[[], Awaitable[str]],
        *,
        is_done: Callable[[], bool] | None = None,
        raise_if_busy: bool = True,
        timeout: float = 30,
        interval: float = 2,
    ) -> None:
        """Run one command, blocking duplicate commands until fresh condition data arrives."""
        if self._command_in_progress:
            if raise_if_busy:
                raise HomeAssistantError("A Deepal command is already in progress; wait for vehicle data to refresh")
            return

        self._command_in_progress = True
        try:
            previous_last_updated = self._condition_last_updated()
            command_id = await send_command()
            if self.vehicle_uses_mqtt:
                # MQTT vehicles have no REST condition to poll; read fresh telemetry once.
                await self.async_request_refresh()
                return
            await self.async_poll_command_update(
                command_id,
                previous_last_updated=previous_last_updated,
                is_done=is_done,
                timeout=timeout,
                interval=interval,
            )
        finally:
            self._command_in_progress = False

    async def async_poll_command_update(
        self,
        command_id: str,
        *,
        previous_last_updated: str | None,
        is_done: Callable[[], bool] | None = None,
        timeout: float = 30,
        interval: float = 2,
    ) -> None:
        """Poll command result and condition until the cloud reports fresh data."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        condition_changed = False

        while True:
            result = await self.client.control_result(vehicle_id=self.vehicle_id, command_id=command_id)
            condition = await self.client.condition(self.vehicle_id)
            self._async_set_condition(condition)

            last_updated = self._condition_last_updated(condition)
            condition_changed = bool(last_updated) and last_updated != previous_last_updated
            state_done = is_done() if is_done is not None else True
            if condition_changed and state_done:
                return

            result_code = result.get("resultCode")
            if result_code not in (None, -100, 0, 1015):
                raise HomeAssistantError(f"Deepal command failed with result code {result_code}: {result.get('errorMsg')}")

            if loop.time() >= deadline:
                if not condition_changed:
                    _LOGGER.warning(
                        "Deepal command %s timed out before lastUpdatedAt changed from %s",
                        command_id,
                        previous_last_updated,
                    )
                return

            await asyncio.sleep(interval)

    def _async_set_condition(self, condition: dict[str, Any]) -> None:
        """Update coordinator data with a freshly fetched condition payload."""
        data = dict(self.data or {})
        data["condition"] = condition
        self.async_set_updated_data(data)

    def _condition_last_updated(self, condition: dict[str, Any] | None = None) -> str | None:
        """Return the condition last-updated marker as a stable string."""
        if condition is None:
            condition = (self.data or {}).get("condition") or {}
        value = condition.get("lastUpdatedAt")
        if value is None:
            value = (condition.get("vehicleStatus") or {}).get("lastUpdatedAt")
        return str(value) if value is not None else None

    def _record_refresh_failure(self, err: Exception) -> None:
        """Track and log failed API refreshes for rate-limit observation."""
        self.refresh_failure_count += 1
        self.last_refresh_failure = str(err)
        _LOGGER.warning(
            "Deepal refresh failed for vehicle %s; failure_count=%s; error=%s",
            self.vehicle_id,
            self.refresh_failure_count,
            err,
        )
