"""Time entities for Deepal vehicles."""

from __future__ import annotations

from datetime import time
from typing import Any

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import DeepalApiError, DeepalCommandAuthError, DeepalCommandNotReady
from .coordinator import DeepalDataUpdateCoordinator
from .entity import DeepalEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: DeepalDataUpdateCoordinator = entry.runtime_data
    async_add_entities(
        [
            DeepalChargeScheduleTime(coordinator, "charge_schedule_start_time", "Charge schedule start", "startTime"),
            DeepalChargeScheduleTime(coordinator, "charge_schedule_end_time", "Charge schedule end", "endTime"),
        ]
    )


def _charge_plan(condition: dict[str, Any]) -> dict[str, Any]:
    plans = ((condition.get("charge") or {}).get("chargePlanList") or [])
    return plans[0] if plans and isinstance(plans[0], dict) else {}


def _parse_hhmm(value: Any) -> time | None:
    if value is None:
        return None
    text = str(value).zfill(4)
    if len(text) != 4 or not text.isdigit():
        return None
    hour = int(text[:2])
    minute = int(text[2:])
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


def _format_hhmm(value: time) -> str:
    return f"{value.hour:02d}{value.minute:02d}"


class DeepalChargeScheduleTime(DeepalEntity, TimeEntity):
    """Start or end time for the charging schedule."""

    def __init__(
        self,
        coordinator: DeepalDataUpdateCoordinator,
        key: str,
        name: str,
        plan_field: str,
    ) -> None:
        super().__init__(coordinator, key)
        self._attr_name = name
        self._attr_translation_key = key
        self._plan_field = plan_field

    @property
    def native_value(self) -> time | None:
        return _parse_hhmm(_charge_plan(self.condition).get(self._plan_field))

    async def async_set_value(self, value: time) -> None:
        plan = _charge_plan(self.condition)
        if not plan:
            raise HomeAssistantError("Deepal charge schedule plan is not available")
        start_time = str(plan.get("startTime") or "0000")
        end_time = str(plan.get("endTime") or "0000")
        if self._plan_field == "startTime":
            start_time = _format_hhmm(value)
        else:
            end_time = _format_hhmm(value)

        try:
            command_id = await self.coordinator.client.control_charge_schedule(
                vehicle_id=self.coordinator.vehicle_id,
                plan_id=str(plan["planId"]),
                start_time=start_time,
                end_time=end_time,
                enabled=plan.get("startSwitch") == 1 and plan.get("endSwitch") == 1,
                plan_type=int(plan.get("planType") or 1),
                time_format=int(plan.get("timeFormat") or 1),
                time_zone=str(plan.get("timeZone") or "GMT+08:00"),
            )
        except KeyError as err:
            raise HomeAssistantError("Deepal charge schedule plan id is not available") from err
        except DeepalCommandAuthError as err:
            self.raise_command_reauth_required(err)
        except (DeepalApiError, DeepalCommandNotReady) as err:
            raise HomeAssistantError(f"Deepal charge schedule command failed: {err}") from err
        await self.async_poll_command_update(command_id)
