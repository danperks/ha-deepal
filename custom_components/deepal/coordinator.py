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
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import DeepalApiError, DeepalAuthError, DeepalClient
from .const import (
    ACTIVE_CONDITION_REFRESH_INTERVAL,
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

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

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            await self._async_maybe_active_condition_refresh()
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
        vehicle = next((item for item in vehicles if str(item.get("carId")) == self.vehicle_id), None)
        return {"vehicles": vehicles, "vehicle": vehicle or {}, "condition": condition}

    async def _async_fetch(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Fetch vehicle metadata and condition."""
        vehicles = await self.client.vehicles()
        condition = await self.client.condition(self.vehicle_id)
        return vehicles, condition

    async def _async_maybe_active_condition_refresh(self) -> None:
        """Periodically ask the car/cloud for fresh condition data."""
        if not self.client.commands_enabled:
            return

        loop = asyncio.get_running_loop()
        now = loop.time()
        if now < self._next_active_refresh_at:
            return

        self._next_active_refresh_at = now + ACTIVE_CONDITION_REFRESH_INTERVAL
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
