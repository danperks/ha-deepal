"""Number entities for Deepal vehicles."""

from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
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
    async_add_entities([DeepalChargeLimitNumber(coordinator)])


class DeepalChargeLimitNumber(DeepalEntity, NumberEntity):
    """Maximum battery state-of-charge for AC charging."""

    _attr_translation_key = "charge_limit_control"
    _attr_name = "Charge limit"
    _attr_native_min_value = 50
    _attr_native_max_value = 100
    _attr_native_step = 10
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: DeepalDataUpdateCoordinator) -> None:
        super().__init__(coordinator, "charge_limit_control")

    @property
    def native_value(self) -> int | None:
        value = ((self.condition.get("charge") or {}).get("maxSocPercent"))
        return int(value) if isinstance(value, int | float) else None

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self.async_execute_command(
                lambda: self.coordinator.client.control_charge_limit(
                    vehicle_id=self.coordinator.vehicle_id,
                    percentage=int(value),
                )
            )
        except DeepalCommandAuthError as err:
            self.raise_command_reauth_required(err)
        except (DeepalApiError, DeepalCommandNotReady) as err:
            raise HomeAssistantError(f"Deepal charge limit command failed: {err}") from err
