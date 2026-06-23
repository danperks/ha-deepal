"""Lock entity for Deepal doors."""

from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import DeepalApiError, DeepalCommandAuthError, DeepalCommandNotReady
from .coordinator import DeepalDataUpdateCoordinator
from .entity import DeepalEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: DeepalDataUpdateCoordinator = entry.runtime_data
    async_add_entities([DeepalDoorLock(coordinator)])


class DeepalDoorLock(DeepalEntity, LockEntity):
    """Door lock entity. State is read-only unless remote commands are enabled."""

    _attr_translation_key = "doors"
    _attr_name = "Doors"

    def __init__(self, coordinator: DeepalDataUpdateCoordinator) -> None:
        super().__init__(coordinator, "doors_lock")

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def is_locked(self) -> bool | None:
        door: dict[str, Any] = self.condition.get("door") or {}
        driver = door.get("driverLock")
        passenger = door.get("passengerLock")
        if driver is None and passenger is None:
            return None
        return driver == 0 and passenger == 0

    async def async_lock(self, **kwargs: Any) -> None:
        await self._async_control(command="lock", open_value=False)

    async def async_unlock(self, **kwargs: Any) -> None:
        await self._async_control(command="unlock", open_value=True)

    async def _async_control(self, *, command: str, open_value: bool) -> None:
        client = self.coordinator.client
        try:
            command_id = await client.control_doors(
                vehicle_id=self.coordinator.vehicle_id,
                command=command,
                open_value=open_value,
            )
        except DeepalCommandAuthError as err:
            self.raise_command_reauth_required(err)
        except DeepalCommandNotReady as err:
            raise HomeAssistantError(str(err)) from err
        except DeepalApiError as err:
            raise HomeAssistantError(f"Deepal command failed: {err}") from err
        await self.async_poll_command_update(
            command_id,
            is_done=lambda: self.is_locked is (not open_value),
        )
