"""Sensors for Deepal vehicles."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfLength, UnitOfTemperature
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


def _temp_value(data: dict[str, Any], path: tuple[str | int, ...]) -> float | None:
    value = _path_value(data, path)
    return (value / 10) if isinstance(value, int | float) else None


def _millis_to_datetime(value: Any) -> datetime | None:
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(millis / 1000, UTC)


def _charge_schedule(data: dict[str, Any]) -> dict[str, Any]:
    plans = _path_value(data, ("charge", "chargePlanList"))
    if not isinstance(plans, list) or not plans:
        return {}
    first_plan = plans[0]
    return first_plan if isinstance(first_plan, dict) else {}


def _format_hhmm(value: Any) -> str | None:
    if value is None:
        return None
    raw = str(value).zfill(4)
    if len(raw) != 4 or not raw.isdigit():
        return str(value)
    return f"{raw[:2]}:{raw[2:]}"


@dataclass(frozen=True, kw_only=True)
class DeepalSensorDescription(SensorEntityDescription):
    path: tuple[str | int, ...]
    divide_by: float | None = None
    timestamp_ms: bool = False
    options: list[str] | None = None


SENSOR_NAMES = {
    "soc": "Battery",
    "range": "Estimated range",
    "total_mileage": "Total mileage",
    "speed": "Speed",
    "last_updated": "Vehicle data timestamp",
    "inside_temperature": "Inside temperature",
    "outside_temperature": "Outside temperature",
    "cabin_climate_mode": "Cabin climate mode",
    "cabin_humidity": "Cabin humidity",
    "inside_pm25": "Cabin PM2.5",
    "inside_air_quality_level": "Cabin air quality level",
    "charge_status": "Charge status",
    "charge_current": "Reported charge current",
    "ac_charge_current": "AC input current",
    "dc_charge_current": "Battery current",
    "remaining_charge_time": "Remaining charge time",
    "charge_limit": "Charge limit",
    "charge_schedule_start_time": "Charge schedule start time",
    "charge_schedule_end_time": "Charge schedule end time",
    "tire_left_front_pressure": "Left front tire pressure",
    "tire_right_front_pressure": "Right front tire pressure",
    "tire_left_rear_pressure": "Left rear tire pressure",
    "tire_right_rear_pressure": "Right rear tire pressure",
    "driver_seat_heater_level": "Driver seat heater level",
    "front_passenger_seat_heater_level": "Front passenger seat heater level",
    "left_rear_seat_heater_level": "Left rear seat heater level",
    "right_rear_seat_heater_level": "Right rear seat heater level",
    "steering_wheel_heater": "Steering wheel heater",
    "vehicle_status": "Vehicle status",
    "power_status": "Power status",
    "gear_signal": "Gear signal",
    "epb_status": "Electronic parking brake status",
    "vehicle_image_url": "Vehicle image URL",
    "refresh_failure_count": "Refresh failure count",
}


SENSORS: tuple[DeepalSensorDescription, ...] = (
    DeepalSensorDescription(
        key="soc",
        translation_key="soc",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        path=("vehicleStatus", "soc"),
    ),
    DeepalSensorDescription(
        key="range",
        translation_key="range",
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        path=("vehicleStatus", "drvMileage"),
    ),
    DeepalSensorDescription(
        key="total_mileage",
        translation_key="total_mileage",
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.TOTAL_INCREASING,
        path=("vehicleStatus", "totalMileage"),
    ),
    DeepalSensorDescription(
        key="speed",
        translation_key="speed",
        native_unit_of_measurement="km/h",
        device_class=SensorDeviceClass.SPEED,
        state_class=SensorStateClass.MEASUREMENT,
        path=("vehicleStatus", "speed"),
    ),
    DeepalSensorDescription(
        key="last_updated",
        translation_key="last_updated",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        path=("lastUpdatedAt",),
        timestamp_ms=True,
    ),
    DeepalSensorDescription(
        key="inside_temperature",
        translation_key="inside_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        path=("hvac", "insideTemp"),
        divide_by=10,
    ),
    DeepalSensorDescription(
        key="outside_temperature",
        translation_key="outside_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        path=("hvac", "outsideTemp"),
        divide_by=10,
    ),
    DeepalSensorDescription(
        key="cabin_climate_mode",
        translation_key="cabin_climate_mode",
        device_class=SensorDeviceClass.ENUM,
        path=(),
        options=["off", "heat_cool"],
    ),
    DeepalSensorDescription(
        key="cabin_humidity",
        translation_key="cabin_humidity",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
        path=("hvac", "insideHumidity"),
    ),
    DeepalSensorDescription(
        key="inside_pm25",
        translation_key="inside_pm25",
        native_unit_of_measurement="ug/m3",
        state_class=SensorStateClass.MEASUREMENT,
        path=("hvac", "insidePm25"),
    ),
    DeepalSensorDescription(
        key="inside_air_quality_level",
        translation_key="inside_air_quality_level",
        entity_category=EntityCategory.DIAGNOSTIC,
        path=("hvac", "insideAirQualityLevel"),
    ),
    DeepalSensorDescription(
        key="charge_status",
        translation_key="charge_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        path=("charge", "chargeStatus"),
    ),
    DeepalSensorDescription(
        key="charge_current",
        translation_key="charge_current",
        native_unit_of_measurement="A",
        device_class=SensorDeviceClass.CURRENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        path=("charge", "chargeCurrent"),
    ),
    DeepalSensorDescription(
        key="ac_charge_current",
        translation_key="ac_charge_current",
        native_unit_of_measurement="A",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        path=("charge", "acChargeCurrent"),
    ),
    DeepalSensorDescription(
        key="dc_charge_current",
        translation_key="dc_charge_current",
        native_unit_of_measurement="A",
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        path=("charge", "dcChargeCurrent"),
    ),
    DeepalSensorDescription(
        key="remaining_charge_time",
        translation_key="remaining_charge_time",
        native_unit_of_measurement="min",
        state_class=SensorStateClass.MEASUREMENT,
        path=("charge", "remainChargeTime"),
    ),
    DeepalSensorDescription(
        key="charge_limit",
        translation_key="charge_limit",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        path=("charge", "maxSocPercent"),
    ),
    DeepalSensorDescription(
        key="charge_schedule_start_time",
        translation_key="charge_schedule_start_time",
        path=(),
    ),
    DeepalSensorDescription(
        key="charge_schedule_end_time",
        translation_key="charge_schedule_end_time",
        path=(),
    ),
    DeepalSensorDescription(
        key="tire_left_front_pressure",
        translation_key="tire_left_front_pressure",
        native_unit_of_measurement="kPa",
        device_class=SensorDeviceClass.PRESSURE,
        state_class=SensorStateClass.MEASUREMENT,
        path=("tire", "leftFront", "pressure"),
    ),
    DeepalSensorDescription(
        key="tire_right_front_pressure",
        translation_key="tire_right_front_pressure",
        native_unit_of_measurement="kPa",
        device_class=SensorDeviceClass.PRESSURE,
        state_class=SensorStateClass.MEASUREMENT,
        path=("tire", "rightFront", "pressure"),
    ),
    DeepalSensorDescription(
        key="tire_left_rear_pressure",
        translation_key="tire_left_rear_pressure",
        native_unit_of_measurement="kPa",
        device_class=SensorDeviceClass.PRESSURE,
        state_class=SensorStateClass.MEASUREMENT,
        path=("tire", "leftBack", "pressure"),
    ),
    DeepalSensorDescription(
        key="tire_right_rear_pressure",
        translation_key="tire_right_rear_pressure",
        native_unit_of_measurement="kPa",
        device_class=SensorDeviceClass.PRESSURE,
        state_class=SensorStateClass.MEASUREMENT,
        path=("tire", "rightBack", "pressure"),
    ),
    DeepalSensorDescription(
        key="driver_seat_heater_level",
        translation_key="driver_seat_heater_level",
        path=("seat", "rightFront", "level"),
    ),
    DeepalSensorDescription(
        key="front_passenger_seat_heater_level",
        translation_key="front_passenger_seat_heater_level",
        path=("seat", "leftFront", "level"),
    ),
    DeepalSensorDescription(
        key="left_rear_seat_heater_level",
        translation_key="left_rear_seat_heater_level",
        path=("seat", "leftBack", "level"),
    ),
    DeepalSensorDescription(
        key="right_rear_seat_heater_level",
        translation_key="right_rear_seat_heater_level",
        path=("seat", "rightBack", "level"),
    ),
    DeepalSensorDescription(
        key="steering_wheel_heater",
        translation_key="steering_wheel_heater",
        path=(),
    ),
    DeepalSensorDescription(
        key="vehicle_status",
        translation_key="vehicle_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        path=("vehicleStatus", "status"),
    ),
    DeepalSensorDescription(
        key="power_status",
        translation_key="power_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        path=("vehicleStatus", "powerStatus"),
    ),
    DeepalSensorDescription(
        key="gear_signal",
        translation_key="gear_signal",
        entity_category=EntityCategory.DIAGNOSTIC,
        path=("vehicleStatus", "gearSignal"),
    ),
    DeepalSensorDescription(
        key="epb_status",
        translation_key="epb_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        path=("vehicleStatus", "epbSts"),
    ),
    DeepalSensorDescription(
        key="refresh_failure_count",
        translation_key="refresh_failure_count",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.TOTAL_INCREASING,
        path=(),
    ),
    DeepalSensorDescription(
        key="vehicle_image_url",
        translation_key="vehicle_image_url",
        entity_category=EntityCategory.DIAGNOSTIC,
        path=(),
    ),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: DeepalDataUpdateCoordinator = entry.runtime_data
    async_add_entities(
        [*(DeepalSensor(coordinator, description) for description in SENSORS), DeepalRawConditionSensor(coordinator)]
    )


class DeepalSensor(DeepalEntity, SensorEntity):
    """Deepal sensor."""

    entity_description: DeepalSensorDescription

    def __init__(self, coordinator: DeepalDataUpdateCoordinator, description: DeepalSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._attr_name = SENSOR_NAMES.get(description.key)
        if description.options:
            self._attr_options = description.options

    @property
    def native_value(self) -> Any:
        if self.entity_description.key == "cabin_climate_mode":
            return _cabin_climate_mode_state(self.condition)
        if self.entity_description.key == "charge_schedule_start_time":
            return _format_hhmm(_charge_schedule(self.condition).get("startTime"))
        if self.entity_description.key == "charge_schedule_end_time":
            return _format_hhmm(_charge_schedule(self.condition).get("endTime"))
        if self.entity_description.key == "steering_wheel_heater":
            return _steering_wheel_heater_state(self.condition)
        if self.entity_description.key == "refresh_failure_count":
            return self.coordinator.refresh_failure_count
        if self.entity_description.key == "vehicle_image_url":
            return ((self.coordinator.data or {}).get("vehicle") or {}).get("imgUrl")
        if self.entity_description.timestamp_ms:
            return _millis_to_datetime(_path_value(self.condition, self.entity_description.path))
        value = _path_value(self.condition, self.entity_description.path)
        if self.entity_description.divide_by and isinstance(value, int | float):
            return value / self.entity_description.divide_by
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.key == "refresh_failure_count":
            return {"last_failure": self.coordinator.last_refresh_failure}
        if self.entity_description.key == "vehicle_image_url":
            vehicle = ((self.coordinator.data or {}).get("vehicle") or {})
            return {
                "series": vehicle.get("seriesName") or vehicle.get("seriesCode"),
                "model": vehicle.get("modelName") or vehicle.get("modelCode"),
            }
        if self.entity_description.key == "cabin_climate_mode":
            hvac = self.condition.get("hvac") or {}
            return {
                "raw_ac_status": hvac.get("acStatus"),
                "target_temperature": _temp_value(self.condition, ("hvac", "remoteTemp")),
            }
        if self.entity_description.key in ("charge_schedule_start_time", "charge_schedule_end_time"):
            return _charge_schedule_attributes(self.condition)
        if self.entity_description.key == "steering_wheel_heater":
            vehicle_status = self.condition.get("vehicleStatus") or {}
            return {
                "raw_switch": vehicle_status.get("steeringWheelHeater"),
                "raw_level": vehicle_status.get("steeringWheelHeaterLevel"),
                "levels": {"0": "Off", "1": "Low", "2": "Medium", "3": "High"},
            }
        return None


class DeepalRawConditionSensor(DeepalEntity, SensorEntity):
    """Diagnostic sensor exposing the complete condition payload."""

    _attr_translation_key = "raw_condition"
    _attr_name = "Raw condition data"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: DeepalDataUpdateCoordinator) -> None:
        super().__init__(coordinator, "raw_condition")

    @property
    def native_value(self) -> datetime | None:
        return _millis_to_datetime(self.condition.get("lastUpdatedAt"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"condition": self.condition}


def _steering_wheel_heater_state(data: dict[str, Any]) -> str | None:
    vehicle_status = data.get("vehicleStatus") or {}
    heater_on = vehicle_status.get("steeringWheelHeater")
    level = vehicle_status.get("steeringWheelHeaterLevel")
    if heater_on is None and level is None:
        return None
    if not heater_on or not level:
        return "Off"
    if level == 1:
        return "Low"
    if level == 2:
        return "Medium"
    if level == 3:
        return "High"
    return None


def _cabin_climate_mode_state(data: dict[str, Any]) -> str | None:
    ac_status = (data.get("hvac") or {}).get("acStatus")
    if ac_status is None:
        return None
    return "off" if ac_status == 0 else "heat_cool"


def _charge_schedule_attributes(data: dict[str, Any]) -> dict[str, Any]:
    plan = _charge_schedule(data)
    return {
        "plan_id": plan.get("planId"),
        "start_switch": plan.get("startSwitch"),
        "end_switch": plan.get("endSwitch"),
        "is_valid": plan.get("isValid"),
        "weeks": plan.get("weeks"),
        "time_zone": plan.get("timeZone"),
        "time_format": plan.get("timeFormat"),
        "plan_type": plan.get("planType"),
        "send_time": plan.get("sendTime"),
    }
