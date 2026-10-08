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
    entities: list[NumberEntity] = [
        DeepalSeatLevelNumber(coordinator, driver=True, function="heat"),
        DeepalSeatLevelNumber(coordinator, driver=False, function="heat"),
        DeepalSeatLevelNumber(coordinator, driver=True, function="vent"),
        DeepalSeatLevelNumber(coordinator, driver=False, function="vent"),
    ]
    # The S05 accepts the charge-limit command without changing the limit.
    if not coordinator.vehicle_uses_mqtt:
        entities.append(DeepalChargeLimitNumber(coordinator))
    async_add_entities(entities)


class DeepalChargeLimitNumber(DeepalEntity, NumberEntity):
    """Maximum battery state-of-charge for AC charging."""

    _attr_translation_key = "charge_limit_control"
    _attr_name = "Charge limit"
    _attr_native_min_value = 60
    _attr_native_max_value = 100
    _attr_native_step = 1
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


class DeepalSeatLevelNumber(DeepalEntity, NumberEntity):
    """Front seat heating or ventilation level (0 is off)."""

    _attr_native_min_value = 0
    _attr_native_max_value = 3
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: DeepalDataUpdateCoordinator, *, driver: bool, function: str) -> None:
        seat = "driver" if driver else "front_passenger"
        key = f"{seat}_seat_{'heating' if function == 'heat' else 'ventilation'}_control"
        super().__init__(coordinator, key)
        self._attr_translation_key = key
        self._attr_name = f"{'Driver' if driver else 'Front passenger'} seat {'heating' if function == 'heat' else 'ventilation'}"
        self._attr_icon = "mdi:car-seat-heater" if function == "heat" else "mdi:car-seat-cooler"
        self._driver = driver
        self._function = function
        # Condition data reports the driver seat under rightFront.
        self._seat_key = "rightFront" if driver else "leftFront"
        self._status_key = "level" if function == "heat" else "ventStatus"

    @property
    def native_value(self) -> int | None:
        seat: dict[str, Any] = (self.condition.get("seat") or {}).get(self._seat_key) or {}
        level = seat.get(self._status_key)
        return int(level) if isinstance(level, int | float) and 0 <= level <= 3 else None

    async def async_set_native_value(self, value: float) -> None:
        client = self.coordinator.client
        control = client.control_seats_heat if self._function == "heat" else client.control_seats_wind
        level = max(0, min(3, int(value)))
        try:
            await self.async_execute_command(
                lambda: control(vehicle_id=self.coordinator.vehicle_id, driver=self._driver, level=level),
                is_done=lambda: self.native_value == level,
            )
        except DeepalCommandAuthError as err:
            self.raise_command_reauth_required(err)
        except (DeepalApiError, DeepalCommandNotReady) as err:
            raise HomeAssistantError(f"Deepal seat command failed: {err}") from err
