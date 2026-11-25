"""Climate entity for Deepal cabin HVAC."""

from __future__ import annotations

import math
from typing import Any

from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature, HVACMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import DeepalApiError, DeepalCommandAuthError, DeepalCommandNotReady, cabin_humidity_percent
from .coordinator import DeepalDataUpdateCoordinator
from .entity import DeepalEntity


def _tenths_c(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value / 10


class _NativeTemperatures:
    """Temperature properties for Home Assistant 2026.11 and newer.

    That release renames the unit and the current/target temperatures to
    ``native_*`` and deprecates the plain names.
    """

    @property
    def native_temperature_unit(self) -> str:
        return UnitOfTemperature.CELSIUS

    @property
    def native_current_temperature(self) -> float | None:
        return self._current_temperature_c()

    @property
    def native_target_temperature(self) -> float | None:
        return self._target_temperature_c()


class _LegacyTemperatures:
    """Temperature properties for Home Assistant before 2026.11."""

    @property
    def temperature_unit(self) -> str:
        return UnitOfTemperature.CELSIUS

    @property
    def current_temperature(self) -> float | None:
        return self._current_temperature_c()

    @property
    def target_temperature(self) -> float | None:
        return self._target_temperature_c()


# Defining the plain names on 2026.11 logs a deprecation warning, and the
# native names do not exist on 2026.2, so pick one set when the module loads.
_TemperatureProperties = (
    _NativeTemperatures if hasattr(ClimateEntity, "native_target_temperature") else _LegacyTemperatures
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: DeepalDataUpdateCoordinator = entry.runtime_data
    async_add_entities([DeepalClimate(coordinator)])


class DeepalClimate(_TemperatureProperties, DeepalEntity, ClimateEntity):
    """Thermostat-style entity for the car-wide HVAC state."""

    _attr_translation_key = "cabin_climate"
    _attr_name = "Cabin climate"
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT_COOL]
    _attr_target_temperature_step = 0.5
    _attr_min_temp = 16
    _attr_max_temp = 30

    def __init__(self, coordinator: DeepalDataUpdateCoordinator) -> None:
        super().__init__(coordinator, "cabin_climate")

    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE

    def _current_temperature_c(self) -> float | None:
        return _tenths_c((self.condition.get("hvac") or {}).get("insideTemp"))

    def _target_temperature_c(self) -> float | None:
        return _tenths_c((self.condition.get("hvac") or {}).get("remoteTemp"))

    @property
    def hvac_mode(self) -> HVACMode | None:
        hvac = self.condition.get("hvac") or {}
        ac_status = hvac.get("acStatus")
        if ac_status is None:
            return None
        return HVACMode.HEAT_COOL if ac_status != 0 else HVACMode.OFF

    @property
    def current_humidity(self) -> float | None:
        return cabin_humidity_percent((self.condition.get("hvac") or {}).get("insideHumidity"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        hvac = self.condition.get("hvac") or {}
        return {
            "outside_temperature": _tenths_c(hvac.get("outsideTemp")),
            "defrost_status": hvac.get("defrostStatus"),
            "inside_air_quality_level": hvac.get("insideAirQualityLevel"),
            "inside_pm25": hvac.get("insidePm25"),
            "target_temperature_requires_fan": True,
        }

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temperature = kwargs.get("temperature")
        if temperature is None:
            return
        await self._async_send(enabled=True, target_temperature=float(temperature))

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            await self.async_turn_off()
            return
        if hvac_mode == HVACMode.HEAT_COOL:
            await self.async_turn_on()
            return
        raise HomeAssistantError(f"Unsupported HVAC mode: {hvac_mode}")

    async def async_turn_on(self) -> None:
        await self._async_send(enabled=True, target_temperature=self._target_temperature_c() or 21)

    async def async_turn_off(self) -> None:
        await self._async_send(enabled=False, target_temperature=self._target_temperature_c() or 21)

    async def _async_send(self, *, enabled: bool, target_temperature: float) -> None:
        client = self.coordinator.client
        control = (
            client.s05_control_air_conditioner
            if self.coordinator.vehicle_uses_mqtt
            else client.control_air_conditioner
        )
        try:
            await self.async_execute_command(
                lambda: control(
                    vehicle_id=self.coordinator.vehicle_id,
                    enabled=enabled,
                    target_temp_c=target_temperature,
                )
            )
        except DeepalCommandAuthError as err:
            self.raise_command_reauth_required(err)
        except (DeepalApiError, DeepalCommandNotReady) as err:
            raise HomeAssistantError(f"Deepal AC command failed: {err}") from err
