"""Data coordinator for Deepal vehicles."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import DeepalApiError, DeepalAuthError, DeepalClient
from .const import CONF_ACCESS_TOKEN, CONF_REFRESH_TOKEN, DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class DeepalDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetch vehicle list and condition data."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: DeepalClient, vehicle_id: str) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(seconds=entry.options.get("scan_interval", DEFAULT_SCAN_INTERVAL)),
        )
        self.entry = entry
        self.client = client
        self.vehicle_id = vehicle_id
        self.refresh_failure_count = 0
        self.last_refresh_failure: str | None = None

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
        vehicle = next((item for item in vehicles if str(item.get("carId")) == self.vehicle_id), None)
        return {"vehicles": vehicles, "vehicle": vehicle or {}, "condition": condition}

    async def _async_fetch(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Fetch vehicle metadata and condition."""
        vehicles = await self.client.vehicles()
        condition = await self.client.condition(self.vehicle_id)
        return vehicles, condition

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
