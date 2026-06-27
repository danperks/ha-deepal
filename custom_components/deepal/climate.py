"""Climate entity for Deepal cabin HVAC."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature, HVACMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import DeepalApiError, DeepalCommandAuthError, DeepalCommandNotReady
from .coordinator import DeepalDataUpdateCoordinator
from .entity import DeepalEntity


def _tenths_c(value: Any) -> float | None:
    return (value / 10) if isinstance(value, int | float) else None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: DeepalDataUpdateCoordinator = entry.runtime_data
    async_add_entities([DeepalClimate(coordinator)])


class DeepalClimate(DeepalEntity, ClimateEntity):
    """Thermostat-style entity for the car-wide HVAC state."""

    _attr_translation_key = "cabin_climate"
    _attr_name = "Cabin climate"
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT_COOL]
    _attr_target_temperature_step = 0.5
    _attr_min_temp = 16
    _attr_max_temp = 30

    def __init__(self, coordinator: DeepalDataUpdateCoordinator) -> None:
        super().__init__(coordinator, "cabin_climate")

    @property
    def temperature_unit(self) -> str:
        return UnitOfTemperature.CELSIUS

    @property
    def supported_features(self) -> ClimateEntityFeature:
        return (
            ClimateEntityFeature(0)
            if self.coordinator.vehicle_uses_mqtt
            else ClimateEntityFeature.TARGET_TEMPERATURE
        )

    @property
    def hvac_mode(self) -> HVACMode | None:
        hvac = self.condition.get("hvac") or {}
        ac_status = hvac.get("acStatus")
        if ac_status is None:
            return None
        return HVACMode.HEAT_COOL if ac_status != 0 else HVACMode.OFF

    @property
    def current_temperature(self) -> float | None:
        return _tenths_c((self.condition.get("hvac") or {}).get("insideTemp"))

    @property
    def target_temperature(self) -> float | None:
        return _tenths_c((self.condition.get("hvac") or {}).get("remoteTemp"))

    @property
    def current_humidity(self) -> float | None:
        humidity = (self.condition.get("hvac") or {}).get("insideHumidity")
        return float(humidity) if isinstance(humidity, int | float) else None

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
        self._raise_if_read_only()
        temperature = kwargs.get("temperature")
        if temperature is None:
            return
        await self._async_send(enabled=True, target_temperature=float(temperature))

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self._raise_if_read_only()
        if hvac_mode == HVACMode.OFF:
            await self.async_turn_off()
            return
        if hvac_mode == HVACMode.HEAT_COOL:
            await self.async_turn_on()
            return
        raise HomeAssistantError(f"Unsupported HVAC mode: {hvac_mode}")

    async def async_turn_on(self) -> None:
        self._raise_if_read_only()
        await self._async_send(enabled=True, target_temperature=self.target_temperature or 21)

    async def async_turn_off(self) -> None:
        self._raise_if_read_only()
        await self._async_send(enabled=False, target_temperature=self.target_temperature or 21)

    def _raise_if_read_only(self) -> None:
        if self.coordinator.vehicle_uses_mqtt:
            raise HomeAssistantError("S05 MQTT vehicles are read-only in this version")

    async def _async_send(self, *, enabled: bool, target_temperature: float) -> None:
        try:
            await self.async_execute_command(
                lambda: self.coordinator.client.control_air_conditioner(
                    vehicle_id=self.coordinator.vehicle_id,
                    enabled=enabled,
                    target_temp_c=target_temperature,
                )
            )
        except DeepalCommandAuthError as err:
            self.raise_command_reauth_required(err)
        except (DeepalApiError, DeepalCommandNotReady) as err:
            raise HomeAssistantError(f"Deepal AC command failed: {err}") from err
