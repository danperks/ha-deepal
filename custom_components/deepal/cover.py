"""Cover entities for Deepal windows and boot."""

from __future__ import annotations

from typing import Any

from homeassistant.components.cover import CoverDeviceClass, CoverEntity, CoverEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import DeepalApiError, DeepalCommandAuthError, DeepalCommandNotReady
from .coordinator import DeepalDataUpdateCoordinator
from .entity import DeepalEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: DeepalDataUpdateCoordinator = entry.runtime_data
    if coordinator.vehicle_uses_mqtt:
        return
    async_add_entities([DeepalWindowsCover(coordinator), DeepalBootCover(coordinator)])


class _DeepalOpenCloseCover(DeepalEntity, CoverEntity):
    """Base entity for captured open/close commands."""

    _attr_supported_features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._async_control(open_value=True)

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._async_control(open_value=False)

    async def _async_control(self, *, open_value: bool) -> None:
        raise NotImplementedError

class DeepalWindowsCover(_DeepalOpenCloseCover):
    """All-window open/close control."""

    _attr_translation_key = "windows"
    _attr_name = "Windows"
    _attr_device_class = CoverDeviceClass.WINDOW

    def __init__(self, coordinator: DeepalDataUpdateCoordinator) -> None:
        super().__init__(coordinator, "windows_cover")

    @property
    def is_closed(self) -> bool | None:
        windows = (self.condition.get("window") or {}).get("windows")
        if not isinstance(windows, list) or not windows:
            return None
        return all(value == 0 for value in windows)

    async def _async_control(self, *, open_value: bool) -> None:
        try:
            await self.async_execute_command(
                lambda: self.coordinator.client.control_windows(
                    vehicle_id=self.coordinator.vehicle_id,
                    open_value=open_value,
                ),
                is_done=lambda: self.is_closed is (not open_value),
            )
        except DeepalCommandAuthError as err:
            self.raise_command_reauth_required(err)
        except DeepalCommandNotReady as err:
            raise HomeAssistantError(str(err)) from err
        except DeepalApiError as err:
            raise HomeAssistantError(f"Deepal windows command failed: {err}") from err


class DeepalBootCover(_DeepalOpenCloseCover):
    """Boot/trunk open/close control."""

    _attr_translation_key = "boot"
    _attr_name = "Boot"
    _attr_device_class = CoverDeviceClass.GARAGE

    def __init__(self, coordinator: DeepalDataUpdateCoordinator) -> None:
        super().__init__(coordinator, "boot_cover")

    @property
    def is_closed(self) -> bool | None:
        trunk = (self.condition.get("door") or {}).get("trunk")
        return (trunk == 0) if trunk is not None else None

    async def _async_control(self, *, open_value: bool) -> None:
        try:
            await self.async_execute_command(
                lambda: self.coordinator.client.control_trunk(
                    vehicle_id=self.coordinator.vehicle_id,
                    open_value=open_value,
                ),
                is_done=lambda: self.is_closed is (not open_value),
            )
        except DeepalCommandAuthError as err:
            self.raise_command_reauth_required(err)
        except DeepalCommandNotReady as err:
            raise HomeAssistantError(str(err)) from err
        except DeepalApiError as err:
            raise HomeAssistantError(f"Deepal boot command failed: {err}") from err
