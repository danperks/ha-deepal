"""Button entities for Deepal vehicles."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import DeepalApiError, DeepalCommandAuthError, DeepalCommandNotReady
from .coordinator import DeepalDataUpdateCoordinator
from .entity import DeepalEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: DeepalDataUpdateCoordinator = entry.runtime_data
    async_add_entities(
        [
            DeepalRefreshButton(coordinator),
            DeepalFlashLightsButton(coordinator),
            DeepalHonkHornButton(coordinator),
        ]
    )


class DeepalRefreshButton(DeepalEntity, ButtonEntity):
    """Manually refresh cached Deepal cloud vehicle data."""

    _attr_translation_key = "refresh"
    _attr_name = "Refresh vehicle data"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: DeepalDataUpdateCoordinator) -> None:
        super().__init__(coordinator, "refresh")

    async def async_press(self) -> None:
        await self.coordinator.async_request_refresh()


class _DeepalFlashHonkButton(DeepalEntity, ButtonEntity):
    """Base entity for captured flash/honk actions."""

    _action_type: int

    async def async_press(self) -> None:
        try:
            command_id = await self.coordinator.client.control_flashing_honking(
                vehicle_id=self.coordinator.vehicle_id,
                action_type=self._action_type,
            )
        except DeepalCommandAuthError as err:
            self.raise_command_reauth_required(err)
        except DeepalCommandNotReady as err:
            raise HomeAssistantError(str(err)) from err
        except DeepalApiError as err:
            raise HomeAssistantError(f"Deepal command failed: {err}") from err
        await self.async_poll_command_update(command_id)


class DeepalFlashLightsButton(_DeepalFlashHonkButton):
    """Momentary button to flash the vehicle lights."""

    _attr_translation_key = "flash_lights"
    _attr_name = "Flash lights"
    _action_type = 1

    def __init__(self, coordinator: DeepalDataUpdateCoordinator) -> None:
        super().__init__(coordinator, "flash_lights")


class DeepalHonkHornButton(_DeepalFlashHonkButton):
    """Momentary button to sound the horn."""

    _attr_translation_key = "honk_horn"
    _attr_name = "Honk horn"
    _action_type = 3

    def __init__(self, coordinator: DeepalDataUpdateCoordinator) -> None:
        super().__init__(coordinator, "honk_horn")
