"""Binary sensors for Deepal vehicles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity, BinarySensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import DeepalDataUpdateCoordinator
from .entity import DeepalEntity


def _path_value(data: dict[str, Any], path: tuple[str | int, ...]) -> Any:
    value: Any = data
    for key in path:
        if isinstance(key, int):
            if not isinstance(value, list) or len(value) <= key:
                return None
            value = value[key]
            continue
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _door(data: dict[str, Any]) -> dict[str, Any]:
    return data.get("door") or {}


def _charge(data: dict[str, Any]) -> dict[str, Any]:
    return data.get("charge") or {}


def _charge_cable_connected(data: dict[str, Any]) -> bool | None:
    charge = _charge(data)
    charge_status = charge.get("chargeStatus")
    charge_connection = charge.get("chargeConStatus")
    if charge_status not in (None, 0):
        return True
    if charge_connection is None:
        return None
    return charge_connection not in (0, 1)


def _charge_schedule(data: dict[str, Any]) -> dict[str, Any]:
    plans = _path_value(data, ("charge", "chargePlanList"))
    if not isinstance(plans, list) or not plans:
        return {}
    first_plan = plans[0]
    return first_plan if isinstance(first_plan, dict) else {}


def _charge_schedule_enabled(data: dict[str, Any]) -> bool | None:
    plan = _charge_schedule(data)
    if not plan:
        return None
    return bool(plan.get("startSwitch") or plan.get("endSwitch"))


@dataclass(frozen=True, kw_only=True)
class DeepalBinarySensorDescription(BinarySensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], bool | None]


BINARY_SENSOR_NAMES = {
    "any_door_open": "Any door open",
    "front_left_door_open": "Front left door open",
    "front_right_door_open": "Front right door open",
    "rear_left_door_open": "Rear left door open",
    "rear_right_door_open": "Rear right door open",
    "trunk_open": "Trunk open",
    "driver_door_locked": "Driver door locked",
    "passenger_door_locked": "Passenger door locked",
    "front_left_window_open": "Front left window open",
    "front_right_window_open": "Front right window open",
    "rear_left_window_open": "Rear left window open",
    "rear_right_window_open": "Rear right window open",
    "charge_cable_connected": "Charge cable connected",
    "dc_charge_gun_connected": "DC charge gun connected",
    "charging": "Charging",
    "charge_schedule_enabled": "Charge schedule enabled",
    "defrost_on": "Defrost on",
    "connected": "Vehicle connected",
    "engine_on": "Engine on",
    "high_beam_on": "High beam on",
    "low_beam_on": "Low beam on",
    "position_lamp_on": "Position lamp on",
    "left_turn_signal_on": "Left turn signal on",
    "right_turn_signal_on": "Right turn signal on",
    "driver_seat_heater_on": "Driver seat heater on",
    "front_passenger_seat_heater_on": "Front passenger seat heater on",
    "driver_seat_ventilation_on": "Driver seat ventilation on",
    "front_passenger_seat_ventilation_on": "Front passenger seat ventilation on",
}


BINARY_SENSORS: tuple[DeepalBinarySensorDescription, ...] = (
    DeepalBinarySensorDescription(
        key="any_door_open",
        translation_key="any_door_open",
        device_class=BinarySensorDeviceClass.DOOR,
        value_fn=lambda data: any(value != 0 for value in (_door(data).get("doors") or [])),
    ),
    DeepalBinarySensorDescription(
        key="front_left_door_open",
        translation_key="front_left_door_open",
        device_class=BinarySensorDeviceClass.DOOR,
        value_fn=lambda data: _path_value(data, ("door", "doors", 0)) != 0 if _path_value(data, ("door", "doors", 0)) is not None else None,
    ),
    DeepalBinarySensorDescription(
        key="front_right_door_open",
        translation_key="front_right_door_open",
        device_class=BinarySensorDeviceClass.DOOR,
        value_fn=lambda data: _path_value(data, ("door", "doors", 1)) != 0 if _path_value(data, ("door", "doors", 1)) is not None else None,
    ),
    DeepalBinarySensorDescription(
        key="rear_left_door_open",
        translation_key="rear_left_door_open",
        device_class=BinarySensorDeviceClass.DOOR,
        value_fn=lambda data: _path_value(data, ("door", "doors", 2)) != 0 if _path_value(data, ("door", "doors", 2)) is not None else None,
    ),
    DeepalBinarySensorDescription(
        key="rear_right_door_open",
        translation_key="rear_right_door_open",
        device_class=BinarySensorDeviceClass.DOOR,
        value_fn=lambda data: _path_value(data, ("door", "doors", 3)) != 0 if _path_value(data, ("door", "doors", 3)) is not None else None,
    ),
    DeepalBinarySensorDescription(
        key="trunk_open",
        translation_key="trunk_open",
        device_class=BinarySensorDeviceClass.DOOR,
        value_fn=lambda data: (_door(data).get("trunk") != 0) if _door(data).get("trunk") is not None else None,
    ),
    DeepalBinarySensorDescription(
        key="driver_door_locked",
        translation_key="driver_door_locked",
        value_fn=lambda data: (_door(data).get("driverLock") == 0) if _door(data).get("driverLock") is not None else None,
    ),
    DeepalBinarySensorDescription(
        key="passenger_door_locked",
        translation_key="passenger_door_locked",
        value_fn=lambda data: (_door(data).get("passengerLock") == 0) if _door(data).get("passengerLock") is not None else None,
    ),
    DeepalBinarySensorDescription(
        key="front_left_window_open",
        translation_key="front_left_window_open",
        device_class=BinarySensorDeviceClass.WINDOW,
        value_fn=lambda data: _path_value(data, ("window", "windows", 0)) != 0 if _path_value(data, ("window", "windows", 0)) is not None else None,
    ),
    DeepalBinarySensorDescription(
        key="front_right_window_open",
        translation_key="front_right_window_open",
        device_class=BinarySensorDeviceClass.WINDOW,
        value_fn=lambda data: _path_value(data, ("window", "windows", 1)) != 0 if _path_value(data, ("window", "windows", 1)) is not None else None,
    ),
    DeepalBinarySensorDescription(
        key="rear_left_window_open",
        translation_key="rear_left_window_open",
        device_class=BinarySensorDeviceClass.WINDOW,
        value_fn=lambda data: _path_value(data, ("window", "windows", 2)) != 0 if _path_value(data, ("window", "windows", 2)) is not None else None,
    ),
    DeepalBinarySensorDescription(
        key="rear_right_window_open",
        translation_key="rear_right_window_open",
        device_class=BinarySensorDeviceClass.WINDOW,
        value_fn=lambda data: _path_value(data, ("window", "windows", 3)) != 0 if _path_value(data, ("window", "windows", 3)) is not None else None,
    ),
    DeepalBinarySensorDescription(
        key="charge_cable_connected",
        translation_key="charge_cable_connected",
        device_class=BinarySensorDeviceClass.PLUG,
        value_fn=_charge_cable_connected,
    ),
    DeepalBinarySensorDescription(
        key="dc_charge_gun_connected",
        translation_key="dc_charge_gun_connected",
        device_class=BinarySensorDeviceClass.PLUG,
        value_fn=lambda data: (_charge(data).get("dcChargeGunConnectStatus") == 0) if _charge(data).get("dcChargeGunConnectStatus") is not None else None,
    ),
    DeepalBinarySensorDescription(
        key="charging",
        translation_key="charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        value_fn=lambda data: (_charge(data).get("chargeStatus") != 0) if _charge(data).get("chargeStatus") is not None else None,
    ),
    DeepalBinarySensorDescription(
        key="charge_schedule_enabled",
        translation_key="charge_schedule_enabled",
        value_fn=_charge_schedule_enabled,
    ),
    DeepalBinarySensorDescription(
        key="defrost_on",
        translation_key="defrost_on",
        value_fn=lambda data: _path_value(data, ("hvac", "defrostStatus")) != 0 if _path_value(data, ("hvac", "defrostStatus")) is not None else None,
    ),
    DeepalBinarySensorDescription(
        key="connected",
        translation_key="connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: _path_value(data, ("vehicleStatus", "connectStatus")) == 1 if _path_value(data, ("vehicleStatus", "connectStatus")) is not None else None,
    ),
    DeepalBinarySensorDescription(
        key="engine_on",
        translation_key="engine_on",
        value_fn=lambda data: _path_value(data, ("vehicleStatus", "engineSts")) != 0 if _path_value(data, ("vehicleStatus", "engineSts")) is not None else None,
    ),
    DeepalBinarySensorDescription(
        key="high_beam_on",
        translation_key="high_beam_on",
        device_class=BinarySensorDeviceClass.LIGHT,
        value_fn=lambda data: _path_value(data, ("lamp", "highBeam")) != 0 if _path_value(data, ("lamp", "highBeam")) is not None else None,
    ),
    DeepalBinarySensorDescription(
        key="low_beam_on",
        translation_key="low_beam_on",
        device_class=BinarySensorDeviceClass.LIGHT,
        value_fn=lambda data: _path_value(data, ("lamp", "lowBeam")) != 0 if _path_value(data, ("lamp", "lowBeam")) is not None else None,
    ),
    DeepalBinarySensorDescription(
        key="position_lamp_on",
        translation_key="position_lamp_on",
        device_class=BinarySensorDeviceClass.LIGHT,
        value_fn=lambda data: _path_value(data, ("lamp", "positionLamp")) != 0 if _path_value(data, ("lamp", "positionLamp")) is not None else None,
    ),
    DeepalBinarySensorDescription(
        key="left_turn_signal_on",
        translation_key="left_turn_signal_on",
        device_class=BinarySensorDeviceClass.LIGHT,
        value_fn=lambda data: _path_value(data, ("lamp", "leftTurn")) != 0 if _path_value(data, ("lamp", "leftTurn")) is not None else None,
    ),
    DeepalBinarySensorDescription(
        key="right_turn_signal_on",
        translation_key="right_turn_signal_on",
        device_class=BinarySensorDeviceClass.LIGHT,
        value_fn=lambda data: _path_value(data, ("lamp", "rightTurn")) != 0 if _path_value(data, ("lamp", "rightTurn")) is not None else None,
    ),
    DeepalBinarySensorDescription(
        key="driver_seat_heater_on",
        translation_key="driver_seat_heater_on",
        value_fn=lambda data: _path_value(data, ("seat", "rightFront", "heatStatus")) != 0 if _path_value(data, ("seat", "rightFront", "heatStatus")) is not None else None,
    ),
    DeepalBinarySensorDescription(
        key="front_passenger_seat_heater_on",
        translation_key="front_passenger_seat_heater_on",
        value_fn=lambda data: _path_value(data, ("seat", "leftFront", "heatStatus")) != 0 if _path_value(data, ("seat", "leftFront", "heatStatus")) is not None else None,
    ),
    DeepalBinarySensorDescription(
        key="driver_seat_ventilation_on",
        translation_key="driver_seat_ventilation_on",
        value_fn=lambda data: _path_value(data, ("seat", "rightFront", "ventStatus")) != 0 if _path_value(data, ("seat", "rightFront", "ventStatus")) is not None else None,
    ),
    DeepalBinarySensorDescription(
        key="front_passenger_seat_ventilation_on",
        translation_key="front_passenger_seat_ventilation_on",
        value_fn=lambda data: _path_value(data, ("seat", "leftFront", "ventStatus")) != 0 if _path_value(data, ("seat", "leftFront", "ventStatus")) is not None else None,
    ),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: DeepalDataUpdateCoordinator = entry.runtime_data
    async_add_entities(DeepalBinarySensor(coordinator, description) for description in BINARY_SENSORS)


class DeepalBinarySensor(DeepalEntity, BinarySensorEntity):
    """Deepal binary sensor."""

    entity_description: DeepalBinarySensorDescription

    def __init__(self, coordinator: DeepalDataUpdateCoordinator, description: DeepalBinarySensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._attr_name = BINARY_SENSOR_NAMES.get(description.key)

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.condition)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.key != "charge_schedule_enabled":
            return None
        plan = _charge_schedule(self.condition)
        return {
            "plan_id": plan.get("planId"),
            "start_time": plan.get("startTime"),
            "end_time": plan.get("endTime"),
            "start_switch": plan.get("startSwitch"),
            "end_switch": plan.get("endSwitch"),
            "is_valid": plan.get("isValid"),
            "weeks": plan.get("weeks"),
            "time_zone": plan.get("timeZone"),
            "time_format": plan.get("timeFormat"),
            "plan_type": plan.get("planType"),
            "send_time": plan.get("sendTime"),
        }
