"""Switch entities for Deepal vehicles."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import DeepalApiError, DeepalCommandAuthError, DeepalCommandNotReady
from .coordinator import DeepalDataUpdateCoordinator
from .entity import DeepalEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: DeepalDataUpdateCoordinator = entry.runtime_data
    entities: list[SwitchEntity] = [DeepalSteeringWheelHeatSwitch(coordinator)]
    if not coordinator.vehicle_uses_mqtt:
        entities.append(DeepalChargeScheduleSwitch(coordinator))
    async_add_entities(entities)


def _charge_plan(condition: dict[str, Any]) -> dict[str, Any]:
    plans = ((condition.get("charge") or {}).get("chargePlanList") or [])
    return plans[0] if plans and isinstance(plans[0], dict) else {}


class DeepalChargeScheduleSwitch(DeepalEntity, SwitchEntity):
    """Enable or disable the charging schedule."""

    _attr_translation_key = "charge_schedule_control"
    _attr_name = "Charge schedule"

    def __init__(self, coordinator: DeepalDataUpdateCoordinator) -> None:
        super().__init__(coordinator, "charge_schedule_control")

    @property
    def is_on(self) -> bool | None:
        plan = _charge_plan(self.condition)
        if not plan:
            return None
        return plan.get("startSwitch") == 1 and plan.get("endSwitch") == 1

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_update_schedule(enabled=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_update_schedule(enabled=False)

    async def _async_update_schedule(self, *, enabled: bool) -> None:
        plan = _charge_plan(self.condition)
        if not plan:
            raise HomeAssistantError("Deepal charge schedule plan is not available")
        try:
            await self.async_execute_command(
                lambda: self.coordinator.client.control_charge_schedule(
                    vehicle_id=self.coordinator.vehicle_id,
                    plan_id=str(plan["planId"]),
                    start_time=str(plan.get("startTime") or "0000"),
                    end_time=str(plan.get("endTime") or "0000"),
                    enabled=enabled,
                    plan_type=int(plan.get("planType") or 1),
                    time_format=int(plan.get("timeFormat") or 1),
                    time_zone=str(plan.get("timeZone") or "GMT+08:00"),
                )
            )
        except KeyError as err:
            raise HomeAssistantError("Deepal charge schedule plan id is not available") from err
        except DeepalCommandAuthError as err:
            self.raise_command_reauth_required(err)
        except (DeepalApiError, DeepalCommandNotReady) as err:
            raise HomeAssistantError(f"Deepal charge schedule command failed: {err}") from err


class DeepalSteeringWheelHeatSwitch(DeepalEntity, SwitchEntity):
    """Turn the steering wheel heating on or off."""

    _attr_translation_key = "steering_wheel_heating_control"
    _attr_name = "Steering wheel heating"
    _attr_icon = "mdi:steering"

    def __init__(self, coordinator: DeepalDataUpdateCoordinator) -> None:
        super().__init__(coordinator, "steering_wheel_heating_control")

    @property
    def is_on(self) -> bool | None:
        value = (self.condition.get("vehicleStatus") or {}).get("steeringWheelHeater")
        return bool(value) if value is not None else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_control(open_value=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_control(open_value=False)

    async def _async_control(self, *, open_value: bool) -> None:
        try:
            await self.async_execute_command(
                lambda: self.coordinator.client.control_steering_wheel_heat(
                    vehicle_id=self.coordinator.vehicle_id,
                    open_value=open_value,
                ),
                is_done=lambda: self.is_on is open_value,
            )
        except DeepalCommandAuthError as err:
            self.raise_command_reauth_required(err)
        except (DeepalApiError, DeepalCommandNotReady) as err:
            raise HomeAssistantError(f"Deepal steering wheel heating command failed: {err}") from err
