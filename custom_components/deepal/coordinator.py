"""Data coordinator for Deepal vehicles."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import DeepalApiError, DeepalAuthError, DeepalClient, _as_int, normalize_condition_fuel
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ACTIVE_REFRESH_INTERVAL,
    CONF_REFRESH_TOKEN,
    DEFAULT_ACTIVE_REFRESH_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .error_codes import command_failure_message

# After an S05 MQTT bootstrap failure, wait 5, then 15, then 30 minutes before
# calling getConnConf again. REST condition covers telemetry in the meantime.
_MQTT_BACKOFF_SECONDS = (5 * 60, 15 * 60, 30 * 60)

_LOGGER = logging.getLogger(__name__)


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
        self._mqtt_failures = 0
        self._mqtt_retry_after = 0.0
        self._last_img_url: str | None = None

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            vehicles, condition = await self._async_fetch()
        except DeepalAuthError:
            vehicles, condition = await self._async_retry_after_refresh(self._async_fetch)
        except DeepalApiError as err:
            self._record_refresh_failure(err)
            raise UpdateFailed(str(err)) from err

        self.last_refresh_failure = None
        vehicle = self._vehicle_from_list(vehicles)
        if isinstance(condition, dict):
            normalize_condition_fuel(condition)
        self._async_sync_location_permission_issue(condition)
        return {"vehicles": vehicles, "vehicle": vehicle or {}, "condition": condition}

    async def async_authorize_qr_code(self, qr_code_or_url: str) -> dict[str, Any]:
        """Authorise a head-unit location QR code, then refresh vehicle data.

        Token refresh matches ``_async_update_data``. A failed refresh, or a retry
        that is still an auth error, raises ``ConfigEntryAuthFailed``. A retry
        that fails with a normal API error raises ``UpdateFailed``.
        """
        try:
            result = await self.client.authorize_qr_code(qr_code_or_url)
        except DeepalAuthError:
            result = await self._async_retry_after_refresh(
                lambda: self.client.authorize_qr_code(qr_code_or_url)
            )
        ir.async_delete_issue(self.hass, DOMAIN, f"{self.entry.entry_id}_location_permission")
        await self.async_request_refresh()
        return result

    async def _async_refresh_entry_tokens(self) -> None:
        """Refresh tokens once and store them on the config entry."""
        try:
            tokens = await self.client.refresh_tokens()
        except (DeepalApiError, DeepalAuthError) as refresh_err:
            self._record_refresh_failure(refresh_err)
            raise ConfigEntryAuthFailed(f"Authentication failed: {refresh_err}") from refresh_err
        new_data = dict(self.entry.data)
        new_data[CONF_ACCESS_TOKEN] = tokens.access_token
        if tokens.refresh_token:
            new_data[CONF_REFRESH_TOKEN] = tokens.refresh_token
        self.hass.config_entries.async_update_entry(self.entry, data=new_data)

    async def _async_retry_after_refresh(self, operation: Callable[[], Awaitable[Any]]) -> Any:
        """Refresh tokens, then run ``operation`` outside the refresh error handler.

        ``DeepalAuthError`` from the retry still fails the config entry. A normal
        ``DeepalApiError`` (the car being offline, for example) stays
        ``UpdateFailed`` so polling continues.
        """
        await self._async_refresh_entry_tokens()
        try:
            return await operation()
        except DeepalAuthError as retry_err:
            self._record_refresh_failure(retry_err)
            raise ConfigEntryAuthFailed(f"Authentication failed: {retry_err}") from retry_err
        except DeepalApiError as retry_err:
            self._record_refresh_failure(retry_err)
            raise UpdateFailed(str(retry_err)) from retry_err

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

    async def _async_fetch(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Fetch vehicle metadata and condition."""
        vehicles = await self.client.vehicles()
        vehicle = self._vehicle_from_list(vehicles)
        if vehicle is not None:
            self._remember_vehicle_image(vehicle)
        if self._vehicle_uses_mqtt(vehicle or {}):
            condition = await self._async_s05_condition()
        else:
            await self._async_maybe_active_condition_refresh()
            condition = await self.client.condition(self.vehicle_id)
        return vehicles, condition

    async def _async_s05_condition(self) -> dict[str, Any]:
        """Read S05 telemetry, falling back to REST while MQTT is backing off.

        ``DeepalAuthError`` propagates so the coordinator can refresh tokens.
        Any other API error, including ``APIGW_-1_7_01_004`` and a broker
        timeout, uses ``condition()`` and delays the next MQTT attempt.
        """
        now = asyncio.get_running_loop().time()
        if self._mqtt_failures and now < self._mqtt_retry_after:
            return await self.client.condition(self.vehicle_id)
        try:
            condition = await self.client.s05_mqtt_condition(self.vehicle_id)
        except DeepalAuthError:
            raise
        except DeepalApiError as err:
            self._note_mqtt_failure(err, now)
            return await self.client.condition(self.vehicle_id)
        if self._mqtt_failures:
            _LOGGER.info(
                "S05 MQTT telemetry is working again after %s failed attempt(s)",
                self._mqtt_failures,
            )
        self._mqtt_failures = 0
        self._mqtt_retry_after = 0.0
        return condition

    def _note_mqtt_failure(self, err: DeepalApiError, now: float) -> None:
        """Record the next MQTT attempt. The warning has no traceback."""
        self._mqtt_failures += 1
        delay = _MQTT_BACKOFF_SECONDS[min(self._mqtt_failures, len(_MQTT_BACKOFF_SECONDS)) - 1]
        self._mqtt_retry_after = now + delay
        _LOGGER.warning(
            "S05 MQTT telemetry unavailable (%s); using the REST condition and retrying MQTT in %s s",
            err,
            delay,
        )

    def _remember_vehicle_image(self, vehicle: dict[str, Any]) -> None:
        """Keep the last non-empty ``imgUrl`` when a vehicle poll omits it."""
        url = vehicle.get("imgUrl")
        if isinstance(url, str) and url.strip():
            self._last_img_url = url.strip()
            return
        if self._last_img_url:
            vehicle["imgUrl"] = self._last_img_url

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
                raise HomeAssistantError(command_failure_message(result_code, result.get("errorMsg")))

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
        if isinstance(condition, dict):
            normalize_condition_fuel(condition)
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
