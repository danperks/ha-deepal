"""PHEV fuel sensors, telemetry sentinels, tyre warnings, and climate units."""

from __future__ import annotations

import asyncio
import math
import sys
import unittest
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[1])
if sys.path[0] != _ROOT:
    sys.path.insert(0, _ROOT)

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.climate import ClimateEntity
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import PERCENTAGE, UnitOfLength, UnitOfVolume

from custom_components.deepal import binary_sensor as binary_sensor_mod
from custom_components.deepal import climate as climate_mod
from custom_components.deepal import sensor as sensor_mod
from custom_components.deepal.api import (
    _as_float,
    _as_int,
    _s05_condition_from_params,
    cabin_humidity_percent,
    normalize_condition_fuel,
    remaining_charge_minutes,
)
from custom_components.deepal.binary_sensor import _charge_cable_connected
from custom_components.deepal.climate import (
    DeepalClimate,
    _LegacyTemperatures,
    _NativeTemperatures,
)


class _Coordinator:
    def __init__(self, condition: dict | None) -> None:
        self.vehicle_id = "car-1"
        self.data = {"condition": condition or {}, "vehicle": {"carName": "Deepal"}}
        self.listeners: list = []

    def async_add_listener(self, update_callback, context=None):
        self.listeners.append(update_callback)

        def _remove() -> None:
            self.listeners.remove(update_callback)

        return _remove


class _Entry:
    def __init__(self, coordinator: _Coordinator) -> None:
        self.runtime_data = coordinator
        self.unloads: list = []

    def async_on_unload(self, unsub) -> None:
        self.unloads.append(unsub)


def _sensor(key: str, condition: dict):
    description = next(item for item in sensor_mod.SENSORS if item.key == key)
    return sensor_mod.DeepalSensor(_Coordinator(condition), description)


def _binary(key: str):
    return next(item for item in binary_sensor_mod.BINARY_SENSORS if item.key == key)


def _phev_params(**overrides):
    params = {
        "soc": 93,
        "remainedPowerMile": 112,
        "totalOdometer": 2846.0,
        "vehicleTemperature": 19.0,
        "airConditioningSetTemperature": 25.5,
        "innerHumidity": 153.0,
        "remainingFuel": "53",
        "fuelLeftover": "27",
        "remainedOilMile": 495,
        "ChrgSts": 0,
        "acChargeGunConnectionState": 1,
        "dcChargeGunConnectionState": 0,
        "BattACChrgInCurr": 0.0,
        "BattDCChrgInCurr": 0.0,
        "chargDeltMins": 8191,
        "outsideTemperature": 7.5,
        "lfPressureWarning": 0,
        "rfPressureWarning": "1",
        "lrPressureWarning": 0,
        "rrPressureWarning": 2,
    }
    params.update(overrides)
    return params


class NumberHelpersTests(unittest.TestCase):
    def test_non_finite_numbers_are_unknown(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf"), "nan", "NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                self.assertIsNone(_as_float(value))
                self.assertIsNone(_as_int(value))

    def test_ordinary_numbers_pass_through(self) -> None:
        self.assertEqual(_as_float("12.5"), 12.5)
        self.assertEqual(_as_int("12.5"), 12)
        self.assertEqual(_as_int(0), 0)
        self.assertIsNone(_as_float(None))
        self.assertIsNone(_as_int("nope"))

    def test_infinite_report_does_not_abort(self) -> None:
        condition = _s05_condition_from_params(
            {"soc": float("inf"), "totalOdometer": float("nan"), "remainedPowerMile": 300}
        )
        self.assertIsNone(condition["vehicleStatus"]["soc"])
        self.assertIsNone(condition["vehicleStatus"]["totalMileage"])
        self.assertEqual(condition["vehicleStatus"]["drvMileage"], 300)


class PercentAndHumidityTests(unittest.TestCase):
    def test_soc_marker_is_unknown_and_real_soc_is_kept(self) -> None:
        self.assertIsNone(_s05_condition_from_params({"soc": 255})["vehicleStatus"]["soc"])
        self.assertIsNone(_s05_condition_from_params({"soc": -1})["vehicleStatus"]["soc"])
        self.assertEqual(_s05_condition_from_params({"soc": 0})["vehicleStatus"]["soc"], 0)
        self.assertEqual(_s05_condition_from_params({"remainPower": "72"})["vehicleStatus"]["soc"], 72)

    def test_s05_absolute_humidity_becomes_relative_percent(self) -> None:
        # Live S05 PHEV: 153 = 15.3 g/m³ at 19 °C → 94.1 %RH.
        condition = _s05_condition_from_params(_phev_params())
        self.assertEqual(condition["hvac"]["insideHumidity"], 94.1)
        self.assertEqual(condition["hvac"]["insideTemp"], 190.0)

    def test_zero_humidity_is_the_offline_sentinel(self) -> None:
        for raw in (0, 0.0, "0"):
            with self.subTest(raw=raw):
                condition = _s05_condition_from_params(_phev_params(innerHumidity=raw))
                self.assertIsNone(condition["hvac"]["insideHumidity"])

    def test_humidity_without_cabin_temperature_is_unknown(self) -> None:
        condition = _s05_condition_from_params({"innerHumidity": 153})
        self.assertIsNone(condition["hvac"]["insideHumidity"])

    def test_rest_humidity_stays_a_percentage(self) -> None:
        self.assertEqual(cabin_humidity_percent(45), 45.0)
        self.assertEqual(cabin_humidity_percent(94.1), 94.1)
        self.assertIsNone(cabin_humidity_percent(0))
        self.assertIsNone(cabin_humidity_percent(-1))
        self.assertIsNone(cabin_humidity_percent(136))
        self.assertIsNone(cabin_humidity_percent(float("nan")))
        sensor = _sensor("cabin_humidity", {"hvac": {"insideHumidity": 55}})
        self.assertEqual(sensor.native_value, 55)
        self.assertIsNone(_sensor("cabin_humidity", {"hvac": {"insideHumidity": 0}}).native_value)
        self.assertIsNone(_sensor("cabin_humidity", {"hvac": {"insideHumidity": 153}}).native_value)

    def test_soc_sensor_rejects_the_wake_marker(self) -> None:
        self.assertIsNone(_sensor("soc", {"vehicleStatus": {"soc": 255}}).native_value)
        self.assertEqual(_sensor("soc", {"vehicleStatus": {"soc": 0}}).native_value, 0)
        self.assertIsNone(_sensor("soc", {"vehicleStatus": {"soc": float("nan")}}).native_value)


class ChargeSentinelTests(unittest.TestCase):
    def test_charge_time_sentinels(self) -> None:
        parked = _s05_condition_from_params(_phev_params(ChrgSts=0, chargDeltMins=8191))
        self.assertIsNone(parked["charge"]["remainChargeTime"])
        zero = _s05_condition_from_params(_phev_params(ChrgSts=0, chargDeltMins=0))
        self.assertIsNone(zero["charge"]["remainChargeTime"])
        charging = _s05_condition_from_params(_phev_params(ChrgSts=3, chargDeltMins=0))
        self.assertEqual(charging["charge"]["remainChargeTime"], 0)
        estimate = _s05_condition_from_params(_phev_params(ChrgSts=4, chargDeltMins=40))
        self.assertEqual(estimate["charge"]["remainChargeTime"], 40)
        self.assertIsNone(remaining_charge_minutes(8191, actively_charging=True))
        self.assertEqual(remaining_charge_minutes(0, actively_charging=True), 0)
        self.assertIsNone(remaining_charge_minutes(0, actively_charging=False))

    def test_rest_charge_time_sensor_drops_sentinels(self) -> None:
        self.assertIsNone(
            _sensor("remaining_charge_time", {"charge": {"remainChargeTime": 8191, "chargeStatus": 6}}).native_value
        )
        self.assertIsNone(
            _sensor("remaining_charge_time", {"charge": {"remainChargeTime": 0, "chargeStatus": 0}}).native_value
        )
        self.assertEqual(
            _sensor("remaining_charge_time", {"charge": {"remainChargeTime": 0, "chargeStatus": 6}}).native_value,
            0,
        )

    def test_dc_wake_sentinel_is_unknown(self) -> None:
        condition = _s05_condition_from_params({"BattDCChrgInCurr": -400.0})
        self.assertIsNone(condition["charge"]["dcChargeCurrent"])
        self.assertIsNone(condition["charge"]["chargeCurrent"])
        self.assertNotEqual(condition["charge"]["dcChargeCurrent"], 400)
        stopped = _s05_condition_from_params({"ChrgSts": 0, "BattDCChrgInCurr": -400})
        self.assertIsNone(stopped["charge"]["dcChargeCurrent"])
        self.assertIsNone(_sensor("dc_charge_current", {"charge": {"dcChargeCurrent": -400.0}}).native_value)

    def test_negative_charging_current_is_a_magnitude(self) -> None:
        condition = _s05_condition_from_params(
            {"ChrgSts": 4, "BattACChrgInCurr": 0, "BattDCChrgInCurr": -60.5}
        )
        self.assertEqual(condition["charge"]["dcChargeCurrent"], 60.5)
        self.assertEqual(condition["charge"]["chargeCurrent"], 60.5)
        self.assertEqual(condition["charge"]["acChargeCurrent"], 0.0)
        ac = _s05_condition_from_params({"ChrgSts": 3, "BattACChrgInCurr": -16, "BattDCChrgInCurr": 0})
        self.assertEqual(ac["charge"]["acChargeCurrent"], 16.0)
        self.assertEqual(ac["charge"]["chargeCurrent"], 16.0)

    def test_glitch_current_is_dropped(self) -> None:
        condition = _s05_condition_from_params({"ChrgSts": 4, "BattDCChrgInCurr": 2538})
        self.assertIsNone(condition["charge"]["dcChargeCurrent"])

    def test_stopped_and_stale_cable_latch(self) -> None:
        latched = _s05_condition_from_params(
            {"ChrgSts": 0, "acChargeGunConnectionState": 1, "dcChargeGunConnectionState": 0}
        )
        self.assertEqual(latched["charge"]["chargeStatus"], 0)
        self.assertEqual(latched["charge"]["chargeConStatus"], 0)
        self.assertFalse(_charge_cable_connected(latched))
        self.assertFalse(_binary("charging").value_fn(latched))

        stopped = _s05_condition_from_params({"ChrgSts": 5, "BattACChrgInCurr": 12, "BattDCChrgInCurr": 30})
        self.assertEqual(stopped["charge"]["chargeStatus"], 0)
        self.assertEqual(stopped["charge"]["chargeConStatus"], 3)
        self.assertEqual(stopped["charge"]["chargeCurrent"], 0.0)
        self.assertTrue(_charge_cable_connected(stopped))
        self.assertFalse(_binary("charging").value_fn(stopped))

        plugged = _s05_condition_from_params({"ChrgSts": 1, "BattACChrgInCurr": 5})
        self.assertEqual(plugged["charge"]["chargeStatus"], 0)
        self.assertTrue(_charge_cable_connected(plugged))
        self.assertFalse(_binary("charging").value_fn(plugged))
        self.assertEqual(plugged["charge"]["dcChargeGunConnectStatus"], 1)

        dc = _s05_condition_from_params({"ChrgSts": 4, "BattDCChrgInCurr": 80})
        self.assertEqual(dc["charge"]["chargeStatus"], 4)
        self.assertTrue(_binary("charging").value_fn(dc))
        self.assertTrue(_charge_cable_connected(dc))
        self.assertEqual(dc["charge"]["dcChargeGunConnectStatus"], 0)
        self.assertTrue(_binary("dc_charge_gun_connected").value_fn(dc))

        waiting = _s05_condition_from_params({"ChrgSts": 8})
        self.assertEqual(waiting["charge"]["chargeStatus"], 0)
        self.assertTrue(_charge_cable_connected(waiting))
        self.assertFalse(_binary("charging").value_fn(waiting))

    def test_outside_temperature_is_stored_in_tenths(self) -> None:
        self.assertEqual(_s05_condition_from_params({"outsideTemperature": 21.5})["hvac"]["outsideTemp"], 215.0)
        self.assertEqual(_s05_condition_from_params({"externalTemperature": 18})["hvac"]["outsideTemp"], 180.0)
        self.assertIsNone(_s05_condition_from_params({"soc": 80})["hvac"]["outsideTemp"])


class FuelSensorTests(unittest.TestCase):
    def test_phev_mqtt_fuel_fields(self) -> None:
        condition = _s05_condition_from_params(_phev_params())
        self.assertEqual(condition["fuel"]["levelPercent"], 53)
        self.assertEqual(condition["fuel"]["volumeL"], 27.0)
        self.assertEqual(condition["fuel"]["remainingRangeKm"], 495)
        self.assertNotIn("tankVolume", condition["fuel"])
        self.assertNotIn("tank_capacity", condition["fuel"])

    def test_bev_has_no_fuel_block(self) -> None:
        condition = _s05_condition_from_params({"soc": 80, "totalOdometer": 10, "ChrgSts": 0})
        self.assertNotIn("fuel", condition)

    def test_invalid_fuel_level_does_not_invent_a_block(self) -> None:
        condition = _s05_condition_from_params({"remainingFuel": 255})
        self.assertNotIn("fuel", condition)

    def test_rest_fuel_aliases_are_normalised(self) -> None:
        condition = {
            "vehicleStatus": {"soc": 40, "remainingFuel": "61"},
            "fuel": {"leftVolume": 30, "fuelWltcRemainingMileage": 410, "tankVolume": 51},
        }
        normalize_condition_fuel(condition)
        self.assertEqual(condition["fuel"]["levelPercent"], 61)
        self.assertEqual(condition["fuel"]["volumeL"], 30.0)
        self.assertEqual(condition["fuel"]["remainingRangeKm"], 410)
        self.assertNotIn("tankVolume", condition["fuel"])

    def test_s07_without_fuel_is_unchanged(self) -> None:
        condition = {"vehicleStatus": {"soc": 80}, "tire": {"leftFront": {"pressure": 240}}}
        normalize_condition_fuel(condition)
        self.assertNotIn("fuel", condition)

    def test_descriptions_use_the_requested_units(self) -> None:
        level, volume, distance = (
            next(item for item in sensor_mod.SENSORS if item.key == key)
            for key in ("fuel_level", "fuel_volume", "fuel_range")
        )
        self.assertEqual(level.native_unit_of_measurement, PERCENTAGE)
        self.assertEqual(volume.native_unit_of_measurement, UnitOfVolume.LITERS)
        self.assertEqual(volume.device_class, SensorDeviceClass.VOLUME_STORAGE)
        self.assertEqual(distance.native_unit_of_measurement, UnitOfLength.KILOMETERS)
        self.assertEqual(distance.device_class, SensorDeviceClass.DISTANCE)
        self.assertFalse(any("tank" in item.key for item in sensor_mod.SENSORS))

    def test_fuel_entities_follow_the_data(self) -> None:
        coordinator = _Coordinator({"vehicleStatus": {"soc": 80}})
        entry = _Entry(coordinator)
        added: list = []

        asyncio.run(sensor_mod.async_setup_entry(None, entry, added.extend))
        keys = {entity._key for entity in added}
        self.assertNotIn("fuel_level", keys)
        self.assertNotIn("fuel_volume", keys)
        self.assertNotIn("fuel_range", keys)
        self.assertIn("soc", keys)

        coordinator.data["condition"] = _s05_condition_from_params(_phev_params())
        coordinator.listeners[0]()
        fuel_keys = {entity._key for entity in added if entity._key.startswith("fuel_")}
        self.assertEqual(fuel_keys, {"fuel_level", "fuel_volume", "fuel_range"})
        self.assertEqual(_sensor("fuel_level", coordinator.data["condition"]).native_value, 53)
        self.assertEqual(_sensor("fuel_volume", coordinator.data["condition"]).native_value, 27.0)
        self.assertEqual(_sensor("fuel_range", coordinator.data["condition"]).native_value, 495)

        before = len(added)
        coordinator.listeners[0]()
        self.assertEqual(len(added), before)

    def test_one_fuel_field_adds_only_that_sensor(self) -> None:
        coordinator = _Coordinator({})
        entry = _Entry(coordinator)
        added: list = []
        asyncio.run(sensor_mod.async_setup_entry(None, entry, added.extend))
        coordinator.data["condition"] = {"fuel": {"volumeL": 12.5, "levelPercent": None, "remainingRangeKm": None}}
        coordinator.listeners[0]()
        fuel_keys = {entity._key for entity in added if str(entity._key).startswith("fuel_")}
        self.assertEqual(fuel_keys, {"fuel_volume"})


class TyreWarningTests(unittest.TestCase):
    def test_warnings_are_problem_sensors(self) -> None:
        expected = {
            "tire_left_front_warning": "leftFront",
            "tire_right_front_warning": "rightFront",
            "tire_left_rear_warning": "leftBack",
            "tire_right_rear_warning": "rightBack",
        }
        for key in expected:
            description = _binary(key)
            self.assertEqual(description.device_class, BinarySensorDeviceClass.PROBLEM)

        mapped = _s05_condition_from_params(_phev_params())
        self.assertFalse(_binary("tire_left_front_warning").value_fn(mapped))
        self.assertTrue(_binary("tire_right_front_warning").value_fn(mapped))
        self.assertFalse(_binary("tire_left_rear_warning").value_fn(mapped))
        self.assertTrue(_binary("tire_right_rear_warning").value_fn(mapped))

    def test_missing_warning_is_unknown_and_rest_warning_is_read(self) -> None:
        empty = {"tire": {"leftFront": {"pressure": 240}}}
        self.assertIsNone(_binary("tire_left_front_warning").value_fn(empty))
        rest = {
            "tire": {
                "leftFront": {"warning": 0},
                "rightFront": {"warning": 1},
                "leftBack": {"warning": None},
                "rightBack": {"pressure": 250},
            }
        }
        self.assertFalse(_binary("tire_left_front_warning").value_fn(rest))
        self.assertTrue(_binary("tire_right_front_warning").value_fn(rest))
        self.assertIsNone(_binary("tire_left_rear_warning").value_fn(rest))
        self.assertIsNone(_binary("tire_right_rear_warning").value_fn(rest))


class _FakeTemperatures:
    def _current_temperature_c(self):
        return 19.5

    def _target_temperature_c(self):
        return 22.0


class ClimateCompatibilityTests(unittest.TestCase):
    def test_mixins_expose_one_naming_scheme_each(self) -> None:
        native = type("Car", (_NativeTemperatures, _FakeTemperatures), {})()
        self.assertEqual(native.native_temperature_unit, "°C")
        self.assertEqual(native.native_current_temperature, 19.5)
        self.assertEqual(native.native_target_temperature, 22.0)
        for plain in ("temperature_unit", "current_temperature", "target_temperature"):
            self.assertFalse(hasattr(_NativeTemperatures, plain))

        legacy = type("Car", (_LegacyTemperatures, _FakeTemperatures), {})()
        self.assertEqual(legacy.temperature_unit, "°C")
        self.assertEqual(legacy.current_temperature, 19.5)
        self.assertEqual(legacy.target_temperature, 22.0)
        for native_name in ("native_temperature_unit", "native_current_temperature", "native_target_temperature"):
            self.assertFalse(hasattr(_LegacyTemperatures, native_name))

    def test_entity_follows_this_home_assistant_version(self) -> None:
        if hasattr(ClimateEntity, "native_target_temperature"):
            self.assertTrue(issubclass(DeepalClimate, _NativeTemperatures))
            self.assertFalse(issubclass(DeepalClimate, _LegacyTemperatures))
        else:
            self.assertTrue(issubclass(DeepalClimate, _LegacyTemperatures))
            self.assertFalse(issubclass(DeepalClimate, _NativeTemperatures))

    def test_climate_reads_condition_without_deprecation_names_on_the_wrong_mixin(self) -> None:
        coordinator = _Coordinator(
            {
                "hvac": {
                    "insideTemp": 215,
                    "remoteTemp": 220,
                    "insideHumidity": 40,
                    "outsideTemp": 75,
                    "acStatus": 1,
                }
            }
        )
        entity = DeepalClimate(coordinator)
        self.assertEqual(entity._current_temperature_c(), 21.5)
        self.assertEqual(entity._target_temperature_c(), 22.0)
        self.assertEqual(entity.current_humidity, 40)
        self.assertEqual(entity.extra_state_attributes["outside_temperature"], 7.5)
        coordinator.data["condition"]["hvac"]["insideTemp"] = 180
        coordinator.data["condition"]["hvac"]["insideHumidity"] = 0
        self.assertEqual(entity._current_temperature_c(), 18.0)
        self.assertIsNone(entity.current_humidity)
        self.assertIsNone(DeepalClimate(_Coordinator({"hvac": {"insideHumidity": 153}})).current_humidity)
        self.assertIsNone(DeepalClimate(_Coordinator({"hvac": {"insideTemp": float("nan")}}))._current_temperature_c())
        if not hasattr(ClimateEntity, "native_target_temperature"):
            self.assertEqual(entity.current_temperature, 18.0)
            self.assertEqual(entity.target_temperature, 22.0)
            self.assertEqual(entity.temperature_unit, "°C")
            self.assertFalse(math.isnan(entity.current_temperature))


if __name__ == "__main__":
    unittest.main()
