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
    if coordinator.vehicle_uses_mqtt:
        return
    async_add_entities([DeepalChargeScheduleSwitch(coordinator)])


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
